"""Qualification output validation and semantic dispatch."""

from __future__ import annotations

import hashlib
import tempfile
import time
from collections.abc import Mapping
from pathlib import Path

from .contracts import (
    _DIGEST,
    _MEDIA_TYPE,
    _NAME,
    ArtifactTransferClient,
    FixtureError,
    FixtureObservationUnknown,
    RecipeFixture,
)
from .media import (
    _ffprobe_metadata,
    _glb_metadata,
    _png_metadata,
    _safe_zip_entries,
    _validate_magic,
    _verify_media_decode,
    _wav_metadata,
    media_budget,
)
from .output_values import (
    _assert_number_range,
    _assert_required_keys,
    _parse_fraction,
    _parse_json_object,
)
from .semantic import (
    _validate_document_archive,
    _validate_realtime_transcript,
    _validate_synchronized_media_receipt,
)
from .values import (
    _integer_value,
    _number_value,
    _numeric_token,
    _object,
    _string_list_value,
)


def validate_outputs(
    recipe: RecipeFixture,
    result: Mapping[str, object],
    client: ArtifactTransferClient,
    *,
    timeout_seconds: float = 180,
) -> dict[str, object]:
    deadline = time.monotonic() + timeout_seconds

    def remaining() -> float:
        budget = deadline - time.monotonic()
        if budget <= 0:
            raise FixtureObservationUnknown(
                "output evaluation observation deadline elapsed"
            )
        return budget

    raw_outputs = result.get("output_files")
    if not isinstance(raw_outputs, list) or not raw_outputs:
        raise FixtureObservationUnknown("artifact output projection is unavailable")
    outputs: list[dict[str, object]] = []
    output_metadata: list[tuple[str, str, int, str]] = []
    for raw in raw_outputs:
        if not isinstance(raw, Mapping):
            raise FixtureObservationUnknown("artifact output projection is malformed")
        item = raw
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
            raise FixtureObservationUnknown("artifact output metadata is unavailable")
        outputs.append(dict(item))
        output_metadata.append((name, media_type, size, digest))
    if len(outputs) > recipe.output_limits["max_files"] or len(
        {name for name, _, _, _ in output_metadata}
    ) != len(outputs):
        raise FixtureError("artifact output file identity exceeds its contract")
    if (
        any(
            size > recipe.output_limits["max_file_bytes"]
            or media_type not in recipe.output_limits["allowed_media_types"]
            for _, media_type, size, _ in output_metadata
        )
        or sum(size for _, _, size, _ in output_metadata)
        > recipe.output_limits["max_total_bytes"]
    ):
        raise FixtureError("artifact output media-type or size exceeds its limits")
    with tempfile.TemporaryDirectory(prefix="vonk-qualification-results-") as root:
        contents: list[tuple[dict[str, object], bytes, Path]] = []
        for index, (item, metadata) in enumerate(zip(outputs, output_metadata)):
            name, media_type, size, digest = metadata
            destination = Path(root) / f"{index:02d}-{name}"
            client.download_file(
                f"/api/artifact-jobs/{result['id']}/results/{name}/{digest}",
                destination,
                media_type=media_type,
                expected_sha256=digest,
                expected_size=size,
                overwrite=False,
            )
            content = destination.read_bytes()
            if len(content) != size or hashlib.sha256(content).hexdigest() != digest:
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
            if kind == "media-type":
                allowed_assertion_media_types = _string_list_value(
                    assertion, "allowed", "media type assertion"
                )
                if any(
                    item["media_type"] not in allowed_assertion_media_types
                    for item, _, _ in contents
                ):
                    raise FixtureError("artifact output media-type assertion failed")
            if kind == "minimum-bytes":
                minimum_bytes = _integer_value(
                    assertion, "value", "minimum bytes assertion"
                )
                if any(len(content) < minimum_bytes for _, content, _ in contents):
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
                    "document-archive",
                    "realtime-transcript",
                    "json-document",
                    "jsonl-records",
                    "synchronized-media-receipt",
                }
                and not selected
            ):
                raise FixtureError("artifact semantic assertion selected no output")
            if kind == "media-type-counts":
                declared_counts = _object(assertion.get("counts"), "media type counts")
                actual = {
                    media_type: sum(
                        item["media_type"] == media_type for item, _, _ in contents
                    )
                    for media_type in declared_counts
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
                    with media_budget(remaining()):
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
                    declared_fps = _number_value(
                        assertion, "fps", "video metadata assertion"
                    )
                    fps_tolerance = _number_value(
                        assertion,
                        "fps_tolerance",
                        "video metadata assertion",
                        default=0.01,
                    )
                    if abs(fps - declared_fps) > fps_tolerance:
                        raise FixtureError("artifact MP4 frame-rate assertion failed")
                    if "frame_count" in assertion:
                        try:
                            frame_count = int(str(video.get("nb_read_frames")))
                        except (TypeError, ValueError) as error:
                            raise FixtureObservationUnknown(
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
                                raise FixtureObservationUnknown(
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
                        start_time = _numeric_token(
                            timing, f"artifact MP4 {label} start time"
                        )
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
                            raise FixtureObservationUnknown(
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
                                video_duration_token = video["duration"]
                                audio_duration_token = audio_stream["duration"]
                            except KeyError as error:
                                raise FixtureObservationUnknown(
                                    "artifact MP4 AV duration is unavailable"
                                ) from error
                            video_duration = _numeric_token(
                                video_duration_token, "artifact MP4 AV duration"
                            )
                            audio_duration = _numeric_token(
                                audio_duration_token, "artifact MP4 AV duration"
                            )
                            maximum_delta = _number_value(
                                assertion,
                                "maximum_av_sync_delta_seconds",
                                "video metadata assertion",
                            )
                            if abs(video_duration - audio_duration) > maximum_delta:
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
                    minimum_meshes = _integer_value(
                        assertion, "minimum_meshes", "GLB assertion", default=1
                    )
                    if metadata["mesh_count"] < minimum_meshes:
                        raise FixtureError("artifact GLB mesh assertion failed")
                    minimum_primitives = _integer_value(
                        assertion, "minimum_primitives", "GLB assertion", default=1
                    )
                    if metadata["primitive_count"] < minimum_primitives:
                        raise FixtureError("artifact GLB primitive assertion failed")
            if kind == "zip-entries":
                for _, content, _ in selected:
                    entries = _safe_zip_entries(content)
                    names = [name for name, _ in entries]
                    if "exact_names" in assertion and names != assertion["exact_names"]:
                        raise FixtureError("artifact ZIP file-name assertion failed")
                    if len(entries) < _integer_value(
                        assertion, "minimum_entries", "ZIP assertion", default=1
                    ):
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
            if kind == "document-archive":
                for _, content, _ in selected:
                    _validate_document_archive(content, assertion)
            if kind == "realtime-transcript":
                for _, content, _ in selected:
                    _validate_realtime_transcript(content, assertion)
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
                    if len(records) < _integer_value(
                        assertion, "minimum_records", "JSONL assertion", default=1
                    ):
                        raise FixtureError(
                            "artifact JSONL record-count assertion failed"
                        )
                    for record in records:
                        _assert_required_keys(record, assertion)
            if kind == "synchronized-media-receipt":
                output_contents = {
                    str(item["name"]): content for item, content, _ in contents
                }
                request_profiles = []
                for slot, input_fixture in recipe.inputs:
                    if slot != "request":
                        continue
                    request = _parse_json_object(
                        input_fixture.content, "qualification request"
                    )
                    request_profiles.append(request.get("profile"))
                declared_profile = str(assertion["profile"])
                if assertion["profile_from_request"]:
                    if len(request_profiles) > 1 or any(
                        profile not in assertion["allowed_profiles"]
                        for profile in request_profiles
                    ):
                        raise FixtureError("qualification profile is invalid")
                    expected_profile = (
                        str(request_profiles[0])
                        if request_profiles
                        else str(assertion["default_profile"])
                    )
                else:
                    if request_profiles:
                        raise FixtureError(
                            "immutable qualification profile cannot be overridden"
                        )
                    expected_profile = declared_profile
                for _, content, _ in selected:
                    _validate_synchronized_media_receipt(
                        content, output_contents, expected_profile, assertion
                    )
    return {
        "assertions": list(recipe.assertions),
        "output_files": outputs,
        "output_manifest_sha256": result.get("output_manifest_sha256"),
    }
