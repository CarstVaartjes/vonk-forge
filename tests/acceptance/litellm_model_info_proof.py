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
* a request with tools, a response format and sampling reaches the upstream
  identically through a route with facts and a route without them.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import ClassVar

from vonk_control.gateway_route_grants import BASE_KEY_ROUTES
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


class Upstream(BaseHTTPRequestHandler):
    received: ClassVar[list[dict[str, object]]] = []

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length))
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
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
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


def prove_client_key() -> None:
    """A virtual key, created as the Controller creates them, reads its own facts."""
    scope = [alias_of("plain"), alias_of("options-base")]
    deadline = time.monotonic() + 120
    while True:  # the key store is ready once the database migration finished
        status, created = call(
            "/key/generate",
            {
                "key_alias": "proof-client",
                "models": scope,
                "allowed_routes": list(BASE_KEY_ROUTES),
                "metadata": {"managed_by": "vonk-forge"},
            },
        )
        if status == 200:
            break
        check(time.monotonic() < deadline, f"key generate answered {status}")
        time.sleep(2)
    key = created["key"]  # type: ignore[index]
    status, info = call("/v1/model/info", key=key)
    check(status == 200, f"a client key was refused /v1/model/info: {status} {info}")
    names = sorted(entry["model_name"] for entry in info["data"])  # type: ignore[index]
    check(names == sorted(scope), f"a client key saw {names}, not its own models")
    for path in ("/v2/model/info", "/key/info", "/user/info", "/team/list"):
        status, _ = call(path, key=key)
        check(status in (401, 403), f"a client key reached {path}: {status}")


def prove() -> None:
    prove_client_key()
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
