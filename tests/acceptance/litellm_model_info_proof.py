"""Prove a route's model_info through the real LiteLLM image.

The Controller renders the shared vectors (control/tests/fixtures/route_model_info)
into the exact litellm.json it publishes, one route per vector plus one route
without facts, all pointing at a recording stub upstream. The built LiteLLM
image loads that file, and the proof asserts what a client sees and what the
gateway does:

* ``/v1/model/info`` returns every vector's model_info verbatim, the ``vonk_``
  keys included, and nothing for the route without facts;
* a CLIENT key created as the Controller creates them (its own models and
  ``allowed_routes``) reads ``/v1/model/info`` and sees only its own models,
  while key, user and ``/v2`` information stay refused;
* a ``mode: "decision"`` route loads;
* a prompt far beyond a route's ``max_input_tokens`` is forwarded, not rejected
  by the gateway (the engine is the one gate);
* the audio routes work for an ordinary client key through the official
  OpenAI SDK: ``audio.transcriptions.create`` reaches the upstream as one
  multipart ``POST /v1/audio/transcriptions`` and ``audio.speech.create`` as one
  ``POST /v1/audio/speech`` with exactly the fields sent, the stub's WAV comes
  back unchanged, ``task_type`` is dropped (the LiteLLM v1.103.0 behaviour the
  design relies on, pinned so a bump that changes it fails visibly), the same
  key reads the audio facts from ``/v1/model/info``, a model outside its scope
  is refused, and a key without ``openai_routes`` is refused on both paths;
* a request with tools, a response format and sampling reaches the upstream
  identically through a route with facts and a route without them.
"""

from __future__ import annotations

import argparse
import email.parser
import email.policy
import json
import struct
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import ClassVar, cast

import openai
from vonk_control.gateway_route_grants import BASE_KEY_ROUTES, GatewayInferenceRoute
from vonk_control.litellm import (
    LiteLlmModelInfo,
    LiteLlmPolicy,
    RouteState,
    render_config,
)
from vonk_control.route_bundle_contract import RouteAcceptedModelPolicy

ROOT = Path(__file__).resolve().parents[2]
VECTORS = ROOT / "control/tests/fixtures/route_model_info/vectors.json"
MASTER_KEY = "sk-model-info-proof"
PORT = 4000
DB_PORT = 55432
POSTGRES_IMAGE = json.loads(
    (ROOT / "deploy/compose/images.lock.json").read_text(encoding="utf-8")
)["images"]["postgres"]
DB_PASSWORD = "model-info-proof"


def wav_bytes(seconds: float = 0.1, rate: int = 16000) -> bytes:
    """A small silent mono 16-bit WAV: the stub's speech answer and the upload."""
    data = b"\x00\x00" * int(seconds * rate)
    header = b"RIFF" + struct.pack("<I", 36 + len(data)) + b"WAVEfmt "
    header += struct.pack("<IHHIIHH", 16, 1, 1, rate, rate * 2, 2, 16)
    return header + b"data" + struct.pack("<I", len(data)) + data


STUB_WAV = wav_bytes()
UPLOAD_WAV = wav_bytes(0.25)


def multipart_parts(content_type: str, body: bytes) -> dict[str, bytes]:
    """The parts of a multipart body by field name."""
    message = email.parser.BytesParser(policy=email.policy.HTTP).parsebytes(
        f"Content-Type: {content_type}\r\n\r\n".encode() + body
    )
    return {
        str(part.get_param("name", header="content-disposition")): cast(
            bytes, part.get_payload(decode=True)
        )
        for part in message.iter_parts()
    }


class Upstream(BaseHTTPRequestHandler):
    received: ClassVar[list[dict[str, object]]] = []
    # What the audio routes sent: (path, content type, fields or JSON body).
    audio: ClassVar[list[tuple[str, str, dict[str, object]]]] = []

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length)
        content_type = self.headers.get("Content-Type", "")
        if self.path == "/v1/audio/transcriptions":
            fields: dict[str, object] = dict(multipart_parts(content_type, raw))
            Upstream.audio.append((self.path, content_type, fields))
            self.reply(200, "application/json", b'{"text":"stub transcript"}')
            return
        body = json.loads(raw)
        if self.path == "/v1/audio/speech":
            Upstream.audio.append((self.path, content_type, body))
            self.reply(200, "audio/wav", STUB_WAV)
            return
        Upstream.received.append(body)
        answer = json.dumps(
            {
                "id": "chatcmpl-proof",
                "object": "chat.completion",
                "created": 0,
                "model": body.get("model", ""),
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "ok"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 1,
                    "completion_tokens": 1,
                    "total_tokens": 2,
                },
            }
        ).encode()
        self.reply(200, "application/json", answer)

    def reply(self, status: int, content_type: str, answer: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(answer)))
        self.end_headers()
        self.wfile.write(answer)

    def log_message(self, *_arguments: object) -> None:
        return


def vectors() -> list[dict[str, object]]:
    loaded = json.loads(VECTORS.read_text(encoding="utf-8"))
    assert isinstance(loaded, list)
    return loaded


def alias_of(name: str) -> str:
    return f"proof-{name}"


def render(upstream_host: str, upstream_port: int) -> bytes:
    models: dict[str, RouteAcceptedModelPolicy] = {}
    for vector in vectors():
        name = str(vector["name"])
        models[alias_of(name)] = RouteAcceptedModelPolicy(
            upstream_model=name,
            model_info=LiteLlmModelInfo.model_validate_json(
                json.dumps(vector["model_info"])
            ),
        )
    models[alias_of("plain")] = RouteAcceptedModelPolicy(upstream_model="plain")
    base = f"http://{upstream_host}:{upstream_port}/v1"
    return render_config(
        RouteState(dict.fromkeys(models, base), "0" * 64), LiteLlmPolicy(models)
    )


def call(
    path: str, body: dict[str, object] | None = None, key: str = MASTER_KEY
) -> tuple[int, object]:
    request = urllib.request.Request(
        f"http://127.0.0.1:{PORT}{path}",
        data=None if body is None else json.dumps(body).encode(),
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode(errors="replace")


def wait_healthy(container: str) -> None:
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{PORT}/health/liveliness", timeout=3
            ) as response:
                if response.status == 200:
                    return
        except OSError:
            pass
        time.sleep(2)
    raise SystemExit("LiteLLM did not become healthy with the rendered config")


def check(condition: bool, message: str) -> None:
    if not condition:
        raise SystemExit(f"model_info proof failed: {message}")


def listed(entries: dict[str, dict], alias: str) -> dict:
    check(alias in entries, f"{alias}: route not listed")
    return entries[alias]


CLIENT_SCOPE = [
    alias_of(name)
    for name in ("plain", "options-base", "transcription", "speech-instructions")
]


def generate_key(key_alias: str, routes: list[str]) -> str:
    """A virtual key, created as the Controller creates them (models and routes)."""
    deadline = time.monotonic() + 120
    while True:  # the key store is ready once the database migration finished
        status, created = call(
            "/key/generate",
            {
                "key_alias": key_alias,
                "models": CLIENT_SCOPE,
                "allowed_routes": routes,
                "metadata": {"managed_by": "vonk-forge"},
            },
        )
        if status == 200:
            return created["key"]  # type: ignore[index]
        check(time.monotonic() < deadline, f"key generate answered {status}")
        time.sleep(2)


def prove_client_key() -> str:
    """An ordinary client key reads its own facts; returns it for the audio proof."""
    key = generate_key("proof-client", list(BASE_KEY_ROUTES))
    status, info = call("/v1/model/info", key=key)
    check(status == 200, f"a client key was refused /v1/model/info: {status} {info}")
    names = sorted(entry["model_name"] for entry in info["data"])  # type: ignore[index]
    check(
        names == sorted(CLIENT_SCOPE), f"a client key saw {names}, not its own models"
    )
    for path in ("/v2/model/info", "/key/info", "/user/info", "/team/list"):
        status, _ = call(path, key=key)
        check(status in (401, 403), f"a client key reached {path}: {status}")
    return key


def sdk(key: str) -> openai.OpenAI:
    """The official OpenAI SDK, as any client uses the gateway."""
    return openai.OpenAI(
        base_url=f"http://127.0.0.1:{PORT}/v1", api_key=key, max_retries=0
    )


def refused(action: object) -> int:
    """The status the gateway refused an SDK call with; fails when it did not."""
    try:
        action()  # type: ignore[operator]
    except openai.APIStatusError as error:
        return error.status_code
    raise SystemExit("model_info proof failed: the gateway served a refused call")


def prove_audio(key: str) -> None:
    """The audio routes through the real gateway, with an ordinary client key."""
    client = sdk(key)
    transcription, speech = alias_of("transcription"), alias_of("speech-instructions")

    # The facts every client reads: the modes, endpoints, parameters and the
    # custom keys, verbatim from the shared vectors.
    status, info = call("/v1/model/info", key=key)
    check(status == 200, f"/v1/model/info answered {status} for the client key")
    served = {
        entry["model_name"]: entry["model_info"]
        for entry in info["data"]  # type: ignore[index]
    }
    for vector in vectors():
        name = str(vector["name"])
        if alias_of(name) in (transcription, speech):
            for fact, value in vector["model_info"].items():  # type: ignore[union-attr]
                got = served[alias_of(name)].get(fact)
                check(got == value, f"{name}: the client key sees {fact}={got!r}")
    check(
        served[transcription]["mode"] == "audio_transcription"
        and served[speech]["mode"] == "audio_speech"
        and served[speech]["health_check_voice"] == "alloy"
        and "max_file_size_mb" not in served[transcription],
        "the audio routes do not show their modes",
    )
    status, group = call("/model_group/info", key=key)
    check(status == 200, f"/model_group/info answered {status} for the client key")
    modes = {item["model_group"]: item["mode"] for item in group["data"]}  # type: ignore[index]
    check(
        modes[transcription] == "audio_transcription"
        and modes[speech] == "audio_speech",
        "/model_group/info does not show the audio modes",
    )

    # Speech to text: one multipart POST with the file and the fields sent.
    Upstream.audio.clear()
    result = client.audio.transcriptions.create(
        model=transcription,
        file=("clip.wav", UPLOAD_WAV, "audio/wav"),
        language="nl",
    )
    check(result.text == "stub transcript", f"the transcript is {result.text!r}")
    check(len(Upstream.audio) == 1, "the transcription was not forwarded once")
    path, content_type, fields = Upstream.audio[0]
    check(
        path == "/v1/audio/transcriptions"
        and content_type.startswith("multipart/form-data")
        and fields.get("file") == UPLOAD_WAV
        and fields.get("language") == b"nl"
        and fields.get("model") == b"transcription",
        f"the upstream saw {path} {content_type} {sorted(fields)}",
    )

    # Text to speech: exactly the fields sent, the audio back byte for byte.
    # ``task_type`` is not forwarded by LiteLLM; the design relies on that.
    Upstream.audio.clear()
    answer = client.audio.speech.create(
        model=speech,
        voice="alloy",
        input="The road ahead is closed.",
        response_format="wav",
        instructions="Calm and slow.",
        speed=1.25,
        extra_body={"task_type": "VoiceDesign", "ref_audio": "x", "stream": True},
    )
    check(answer.content == STUB_WAV, "the speech bytes changed on the way back")
    check(
        answer.response.headers["content-type"].startswith("audio/wav"),
        f"the speech content type is {answer.response.headers['content-type']!r}",
    )
    check(len(Upstream.audio) == 1, "the speech request was not forwarded once")
    path, content_type, body = Upstream.audio[0]
    check(
        path == "/v1/audio/speech"
        and content_type.startswith("application/json")
        and body
        == {
            "model": "speech-instructions",
            "voice": "alloy",
            "input": "The road ahead is closed.",
            "response_format": "wav",
            "instructions": "Calm and slow.",
            "speed": 1.25,
        },
        f"the upstream saw {path} {body}",
    )

    # Scope: a model outside the key's list is refused, on both audio paths,
    # before anything reaches the upstream.
    Upstream.audio.clear()
    outside = refused(
        lambda: client.audio.transcriptions.create(
            model=alias_of("transcription-minimal"),
            file=("clip.wav", UPLOAD_WAV, "audio/wav"),
        )
    )
    check(outside in (401, 403), f"an out-of-scope transcription got {outside}")
    outside = refused(
        lambda: client.audio.speech.create(
            model=alias_of("speech"), voice="alloy", input="x"
        )
    )
    check(outside in (401, 403), f"an out-of-scope speech got {outside}")

    # A key without openai_routes reads facts but cannot call either path.
    narrow = sdk(
        generate_key("proof-no-openai-routes", [GatewayInferenceRoute.MODEL_INFO.value])
    )
    for denied in (
        lambda: narrow.audio.transcriptions.create(
            model=transcription, file=("clip.wav", UPLOAD_WAV, "audio/wav")
        ),
        lambda: narrow.audio.speech.create(model=speech, voice="alloy", input="x"),
    ):
        check(refused(denied) == 403, "a key without openai_routes reached audio")
    check(not Upstream.audio, "a refused audio call reached the upstream")


def prove() -> None:
    client_key = prove_client_key()
    entries: dict[str, dict] = {}
    status, info = call("/v1/model/info")
    check(status == 200 and isinstance(info, dict), f"/v1/model/info answered {status}")
    for entry in info["data"]:  # type: ignore[index]
        entries[entry["model_name"]] = entry
    for vector in vectors():
        name = str(vector["name"])
        served = listed(entries, alias_of(name))["model_info"]
        for key, value in vector["model_info"].items():  # type: ignore[union-attr]
            check(served.get(key) == value, f"{name}: {key} is {served.get(key)!r}")
    plain = listed(entries, alias_of("plain"))
    check(
        not any(key.startswith("vonk_") for key in plain["model_info"]),
        "a route without facts shows custom keys",
    )
    decision = entries[alias_of("decision")]["model_info"]
    check(
        decision["mode"] == "decision"
        and decision["supported_endpoints"] == ["/v1/systemone"],
        "the decision route does not show its mode and endpoint",
    )

    # A prompt far beyond max_input_tokens is the engine's to refuse.
    long_prompt = "word " * 6000
    base = entries[alias_of("options-base")]["model_info"]["max_input_tokens"]
    check(base < 6000, "the near-limit route must have a small context")
    Upstream.received.clear()
    status, answer = call(
        "/v1/chat/completions",
        {
            "model": alias_of("options-base"),
            "messages": [{"role": "user", "content": long_prompt}],
            "max_tokens": 8,
        },
    )
    check(status == 200, f"the gateway answered {status}: {answer}")
    check(
        len(Upstream.received) == 1
        and Upstream.received[0]["messages"][0]["content"] == long_prompt,  # type: ignore[index]
        "the long prompt did not reach the upstream unchanged",
    )

    # The same request reaches the upstream identically with and without facts.
    request = {
        "messages": [{"role": "user", "content": "hello"}],
        "max_tokens": 8,
        "temperature": 0.2,
        "tools": [
            {
                "type": "function",
                "function": {"name": "f", "parameters": {"type": "object"}},
            }
        ],
        "response_format": {"type": "json_object"},
    }
    forwarded = []
    for name in ("options-rich", "plain"):
        Upstream.received.clear()
        status, answer = call(
            "/v1/chat/completions", {"model": alias_of(name), **request}
        )
        check(status == 200, f"{name}: the gateway answered {status}: {answer}")
        check(len(Upstream.received) == 1, f"{name}: not forwarded once")
        forwarded.append(
            {k: v for k, v in Upstream.received[0].items() if k != "model"}
        )
    check(forwarded[0] == forwarded[1], "model_info changed the forwarded request")
    check(
        "tools" in forwarded[0] and "response_format" in forwarded[0], "params dropped"
    )
    prove_audio(client_key)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    parser.add_argument("--work", type=Path, required=True)
    parser.add_argument(
        "--published",
        action="store_true",
        help="publish the port instead of host networking (a desktop Docker VM)",
    )
    arguments = parser.parse_args()

    server = ThreadingHTTPServer(
        ("0.0.0.0" if arguments.published else "127.0.0.1", 0), Upstream
    )
    threading.Thread(target=server.serve_forever, daemon=True).start()
    arguments.work.mkdir(parents=True, exist_ok=True)
    config = arguments.work / "litellm.json"
    upstream_host = "host.docker.internal" if arguments.published else "127.0.0.1"
    config.write_bytes(render(upstream_host, server.server_address[1]))
    config.chmod(0o644)
    container = "vonk-model-info-proof"
    database = "vonk-model-info-proof-db"
    for name in (container, database):
        subprocess.run(["docker", "rm", "-f", name], check=False, capture_output=True)
    subprocess.run(
        [
            "docker",
            "run",
            "--detach",
            "--name",
            database,
            "--publish",
            f"127.0.0.1:{DB_PORT}:5432",
            "--env",
            f"POSTGRES_PASSWORD={DB_PASSWORD}",
            "--env",
            "POSTGRES_DB=litellm",
            POSTGRES_IMAGE,
        ],
        check=True,
    )
    database_host = "host.docker.internal" if arguments.published else "127.0.0.1"
    database_url = (
        f"postgresql://postgres:{DB_PASSWORD}@{database_host}:{DB_PORT}/litellm"
    )
    subprocess.run(
        [
            "docker",
            "run",
            "--detach",
            "--name",
            container,
            *(
                ["--publish", f"127.0.0.1:{PORT}:{PORT}"]
                if arguments.published
                else ["--network", "host"]
            ),
            "--env",
            f"LITELLM_MASTER_KEY={MASTER_KEY}",
            "--env",
            "LITELLM_UPSTREAM_KEY=stub-upstream-key",
            "--env",
            f"LITELLM_DATABASE_URL={database_url}",
            "--volume",
            f"{config}:/proof/litellm.json:ro",
            "--entrypoint",
            "litellm",
            arguments.image,
            "--config",
            "/proof/litellm.json",
            "--host",
            "0.0.0.0" if arguments.published else "127.0.0.1",
            "--port",
            str(PORT),
        ],
        check=True,
    )
    try:
        wait_healthy(container)
        prove()
    except BaseException:
        subprocess.run(["docker", "logs", "--tail", "200", container], check=False)
        raise
    finally:
        for name in (container, database):
            subprocess.run(
                ["docker", "rm", "-f", name], check=False, capture_output=True
            )
        server.shutdown()
    print("model_info proof passed")


if __name__ == "__main__":
    sys.exit(main())
