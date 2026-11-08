"""The audio side of canonical serving checks: the upload and the WAV answer.

``openai.transcription`` sends the check's staged fixture as one multipart
``POST /v1/audio/transcriptions``; ``openai.speech`` reads the WAV bytes the
route answers. The recipe contract (``RecipeMultipartServingRequest``,
``speech.audio``) owns what is declared; this module only encodes and measures.
"""

from __future__ import annotations

import secrets
from collections.abc import Mapping
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from vonk_forge_contracts.recipe import WavFacts


class AudioCheckError(ValueError):
    """The declared audio request cannot be built, or the answer is not audio."""


def assert_speech(headers: Mapping[str, str], body: bytes) -> WavFacts:
    """``speech.audio``: an ``audio/*`` response that decodes to enough samples.

    The WAV reader and the minimum length are the recipe contract's own.
    """

    # Imported here: the platform's CLI imports this package without the recipe
    # contracts, which only a recipe's own checks need.
    from vonk_forge_contracts.recipe import MIN_SPEECH_SECONDS, wav_facts

    content_type = next(
        (value for key, value in headers.items() if key.lower() == "content-type"), ""
    )
    if not content_type.split(";", 1)[0].strip().lower().startswith("audio/"):
        raise AudioCheckError(f"the response content type is {content_type!r}")
    audio = wav_facts(body)
    if audio is None:
        raise AudioCheckError("the response is not a decodable WAV")
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
