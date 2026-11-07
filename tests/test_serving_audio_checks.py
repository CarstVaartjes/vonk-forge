"""The audio serving checks: a multipart upload in, a transcript or a WAV out.

The checks come from the recipes contract's shipped example (``recipe-audio-serving``)
read through ``read_recipe``, so the requests are the ones a recipe can declare.
"""

from __future__ import annotations

import email.parser
import email.policy
import json
import struct
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from threading import Thread
from typing import ClassVar

import pytest
from vonk_forge_contracts import read_recipe
from vonk_forge_contracts.api import MAX_AUDIO_FILE_BYTES

from cluster_profiles.serving_audio import (
    AudioCheckError,
    assert_speech,
    multipart_body,
    read_wav,
)
from cluster_profiles.serving_execution import (
    HttpObservation,
    ServingExecutionError,
    ServingObservationUnknown,
    evaluate_http_response,
    execute_http_check,
)

FIXTURE = "qualification/fixtures/jfk-inaugural-10s.wav"


def wav(seconds: float, rate: int = 16000, *, data_size: int | None = None) -> bytes:
    data = b"\x00\x00" * int(seconds * rate)
    header = b"RIFF" + struct.pack("<I", 36 + len(data)) + b"WAVEfmt "
    header += struct.pack("<IHHIIHH", 16, 1, 1, rate, rate * 2, 2, 16)
    size = len(data) if data_size is None else data_size
    return header + b"data" + struct.pack("<I", size) + data


AUDIO = wav(1.0)


def _checks() -> dict[str, dict]:
    document = json.loads(
        files("vonk_forge_contracts")
        .joinpath("examples", "recipe-audio-serving.json")
        .read_text(encoding="utf-8")
    )
    recipe = read_recipe(document)
    return {
        check.name: check.model_dump(mode="json", exclude_none=True)
        for check in recipe.validation.serving.checks
    }


CHECKS = _checks()
TRANSCRIBE = CHECKS["transcribe"]
SPEAK = CHECKS["speak"]


class _Audio(BaseHTTPRequestHandler):
    seen: ClassVar[list[dict]] = []
    transcript: ClassVar[bytes] = b'{"text":"ask not what your country"}'

    def do_POST(self) -> None:
        raw = self.rfile.read(int(self.headers["Content-Length"] or 0))
        content_type = self.headers["Content-Type"]
        if self.path == "/v1/audio/transcriptions":
            message = email.parser.BytesParser(policy=email.policy.HTTP).parsebytes(
                f"Content-Type: {content_type}\r\n\r\n".encode() + raw
            )
            parts = {
                part.get_param("name", header="content-disposition"): (
                    part.get_filename(),
                    part.get_content_type(),
                    part.get_payload(decode=True),
                )
                for part in message.iter_parts()
            }
            type(self).seen.append(parts)
            body, kind = type(self).transcript, "application/json"
        else:
            type(self).seen.append(json.loads(raw))
            body, kind = AUDIO, "audio/wav"
        self.send_response(200)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format: str, *_args: object) -> None:
        return


@pytest.fixture()
def server():
    _Audio.seen = []
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Audio)
    thread = Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_port}"
    finally:
        httpd.shutdown()
        thread.join(timeout=30)


def test_the_transcription_check_uploads_the_staged_fixture_as_multipart(
    server: str,
) -> None:
    observed = execute_http_check(
        server, TRANSCRIBE, model_alias="the-alias", fixtures={FIXTURE: AUDIO}
    )

    assert observed == {"response_shape": "audio.transcription", "characters": 25}
    [parts] = _Audio.seen
    assert parts["file"] == ("jfk-inaugural-10s.wav", "audio/wav", AUDIO)
    # The declared fields go as written; the alias is substituted where named.
    assert parts["language"][2] == b"en"
    assert parts["response_format"][2] == b"json"
    assert parts["model"][2] == b"synthetic-tiny"


def test_the_alias_replaces_a_model_field_that_names_it(server: str) -> None:
    check = json.loads(json.dumps(TRANSCRIBE))
    check["request"]["fields"]["model"] = "$ALIAS"
    execute_http_check(
        server, check, model_alias="the-alias", fixtures={FIXTURE: AUDIO}
    )
    assert _Audio.seen[0]["model"][2] == b"the-alias"


@pytest.mark.parametrize(
    "fixtures",
    [{}, {FIXTURE: b""}, {FIXTURE: b"\x00" * (MAX_AUDIO_FILE_BYTES + 1)}],
    ids=["missing", "empty", "over-the-bound"],
)
def test_an_unusable_fixture_is_an_execution_error_before_any_request(
    fixtures: dict[str, bytes],
) -> None:
    with pytest.raises(ServingExecutionError):
        execute_http_check("http://unused.invalid", TRANSCRIBE, fixtures=fixtures)
    with pytest.raises(AudioCheckError):
        multipart_body({"model": "m"}, "file", "audio/wav", FIXTURE, fixtures)


def _observed(check: dict, body: bytes, kind: str = "application/json"):
    return evaluate_http_response(
        HttpObservation(200, {"Content-Type": kind}, body), check
    )


def test_a_transcript_must_be_nonempty_text() -> None:
    assert _observed(TRANSCRIBE, b'{"text":"hello"}')["characters"] == 5
    for body in (b'{"text":"  "}', b'{"text":3}', b"{}"):
        with pytest.raises(ServingExecutionError, match="empty"):
            _observed(TRANSCRIBE, body)
    with pytest.raises(ServingObservationUnknown):
        _observed(TRANSCRIBE, b"not json")


def test_speech_is_a_wav_of_audible_length() -> None:
    observed = _observed(SPEAK, wav(2.0), "audio/wav")
    assert observed == {
        "response_shape": "audio.speech",
        "audio_seconds": 2.0,
        "sample_rate": 16000,
    }
    # Header case and parameters do not matter; the audio family does.
    assert assert_speech({"content-type": "audio/x-wav; charset=binary"}, wav(1.0))


@pytest.mark.parametrize(
    ("body", "kind"),
    [
        (wav(0.2), "audio/wav"),
        (wav(2.0), "application/json"),
        (wav(2.0), ""),
        (b"ID3" + b"\x00" * 100, "audio/mpeg"),
        (b"RIFF\x00\x00\x00\x00WAVE", "audio/wav"),
        (b"", "audio/wav"),
    ],
    ids=["short", "json-type", "no-type", "mp3", "no-chunks", "empty"],
)
def test_speech_that_is_not_audible_wav_fails_the_check(body: bytes, kind: str) -> None:
    with pytest.raises(ServingExecutionError, match="speech"):
        _observed(SPEAK, body, kind)


def test_a_streamed_wav_with_a_placeholder_size_counts_the_bytes_it_has() -> None:
    audio = read_wav(wav(1.0, data_size=0xFFFFFFFF))
    assert audio.frames == 16000 and audio.seconds == 1.0


def test_a_wav_whose_data_chunk_precedes_fmt_is_refused() -> None:
    body = b"RIFF\x00\x00\x00\x00WAVEdata" + struct.pack("<I", 2) + b"\x00\x00"
    with pytest.raises(AudioCheckError, match="precedes"):
        read_wav(body)


def test_the_speech_check_posts_json_and_reads_the_wav(server: str) -> None:
    observed = execute_http_check(server, SPEAK, model_alias="the-alias")
    assert observed["response_shape"] == "audio.speech"
    assert _Audio.seen[0]["model"] == "synthetic-tiny"
    assert _Audio.seen[0]["voice"] == "alloy"
