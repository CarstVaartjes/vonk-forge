"""The audio side of canonical serving checks: the upload and the WAV answer.

``openai.transcription`` sends the check's staged fixture as one multipart
``POST /v1/audio/transcriptions``; ``openai.speech`` reads the WAV bytes the
route answers. The recipe contract (``RecipeMultipartServingRequest``,
``speech.audio``) owns what is declared; this module only encodes and measures.
"""

from __future__ import annotations

import secrets
import struct
from collections.abc import Mapping
from dataclasses import dataclass

#: ``speech.audio`` asks for at least this much decoded audio.
MIN_SPEECH_SECONDS = 0.5
_RIFF_HEADER = 12
_CHUNK_HEADER = 8
_FMT_MINIMUM = 16


class AudioCheckError(ValueError):
    """The declared audio request cannot be built, or the answer is not audio."""


@dataclass(frozen=True, slots=True)
class WavAudio:
    sample_rate: int
    channels: int
    frames: int

    @property
    def seconds(self) -> float:
        return self.frames / self.sample_rate


def read_wav(body: bytes) -> WavAudio:
    """The format and length of a RIFF/WAVE body with ``fmt `` and ``data`` chunks.

    A ``data`` size that overruns the body (a streamed WAV with a placeholder
    size) counts the bytes that are there.
    """

    if len(body) < _RIFF_HEADER or body[:4] != b"RIFF" or body[8:12] != b"WAVE":
        raise AudioCheckError("the response is not a RIFF/WAVE file")
    fmt: tuple[int, int, int] | None = None
    offset = _RIFF_HEADER
    while offset + _CHUNK_HEADER <= len(body):
        name = body[offset : offset + 4]
        (size,) = struct.unpack_from("<I", body, offset + 4)
        start = offset + _CHUNK_HEADER
        if name == b"fmt ":
            if size < _FMT_MINIMUM or start + _FMT_MINIMUM > len(body):
                raise AudioCheckError("the WAV fmt chunk is truncated")
            _tag, channels, rate, _rate_bytes, align, _bits = struct.unpack_from(
                "<HHIIHH", body, start
            )
            fmt = (channels, rate, align)
        elif name == b"data":
            if fmt is None:
                raise AudioCheckError("the WAV data chunk precedes its fmt chunk")
            channels, rate, align = fmt
            if channels < 1 or rate < 1 or align < 1:
                raise AudioCheckError("the WAV fmt chunk is invalid")
            return WavAudio(rate, channels, min(size, len(body) - start) // align)
        offset = start + size + (size & 1)
    raise AudioCheckError("the WAV has no fmt and data chunks")


def assert_speech(headers: Mapping[str, str], body: bytes) -> WavAudio:
    """``speech.audio``: an ``audio/*`` response that decodes to enough samples."""

    content_type = next(
        (value for key, value in headers.items() if key.lower() == "content-type"), ""
    )
    if not content_type.split(";", 1)[0].strip().lower().startswith("audio/"):
        raise AudioCheckError(f"the response content type is {content_type!r}")
    audio = read_wav(body)
    if audio.seconds < MIN_SPEECH_SECONDS:
        raise AudioCheckError(
            f"the response holds {audio.seconds:.2f} s of audio, under "
            f"{MIN_SPEECH_SECONDS} s"
        )
    return audio


def multipart_body(
    fields: Mapping[str, str],
    file_field: str,
    media_type: str,
    fixture: str,
    fixtures: Mapping[str, bytes],
) -> tuple[bytes, str]:
    """(body, content type) for the form fields plus the staged fixture file.

    The fixture is read from ``fixtures`` (the package's files) and bounded by
    the contract's largest audio upload before it is encoded. The boundary is
    random, so it cannot occur in the content by design.
    """

    # Imported here: the platform's CLI imports this package without the recipe
    # contracts, which only a recipe's own checks need.
    from vonk_forge_contracts.api import MAX_AUDIO_FILE_BYTES

    content = fixtures.get(fixture)
    if content is None:
        raise AudioCheckError(f"the serving fixture {fixture} is not in the package")
    if not 0 < len(content) <= MAX_AUDIO_FILE_BYTES:
        raise AudioCheckError(f"the serving fixture {fixture} has an invalid size")
    boundary = "vonk-" + secrets.token_hex(16)
    marker = boundary.encode()
    parts = [
        b'--%b\r\nContent-Disposition: form-data; name="%b"\r\n\r\n%b\r\n'
        % (marker, name.encode(), value.encode())
        for name, value in fields.items()
    ]
    filename = fixture.rsplit("/", 1)[-1].replace('"', "")
    parts.append(
        b'--%b\r\nContent-Disposition: form-data; name="%b"; filename="%b"\r\n'
        b"Content-Type: %b\r\n\r\n"
        % (marker, file_field.encode(), filename.encode(), media_type.encode())
    )
    parts.append(content)
    parts.append(b"\r\n--%b--\r\n" % marker)
    body = b"".join(parts)
    return body, f"multipart/form-data; boundary={boundary}"
