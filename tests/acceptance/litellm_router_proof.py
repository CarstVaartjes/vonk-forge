"""Prove what the built LiteLLM router does with several members of one alias.

Two controlled upstreams serve one alias; a client key (created as the
Controller creates them) calls it through the official OpenAI SDK. The config is
the Controller's rendering for one route, with the alias widened to two members
and the router settings the load-balancing design names. Two gateway runs:

Run one, no health samples at all (a missing sample counts as eligible):

* simple-shuffle reaches both members, each above zero at N = 20;
* a stream is served by one member from first chunk to last;
* ``num_retries: 0`` leaves a failed member's error with the client: no request
  reaches two members.

Run two, background health on:

* both members report healthy, then both report unhealthy through ``/health``;
* what an all-unhealthy group does is recorded (`ALL_UNHEALTHY_OUTCOME`);
* repeated failures end in cooldown exhaustion, and the status code the client
  gets then is recorded in the log line ``cooldown exhaustion status``;
* once the members answer again, the group serves both without manual action.

The proof only observes the image; it holds no product code.
"""

from __future__ import annotations

import argparse
import enum
import json
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import openai

from vonk_control.gateway.keys import GatewayKeyService
from vonk_control.gateway.keys_contract import GatewayKeyCreated
from vonk_control.gateway.litellm import (
    LiteLlmPolicy,
    RouteState,
    render_config,
)
from vonk_control.gateway.route_bundle_contract import RouteAcceptedModelPolicy

ROOT = Path(__file__).resolve().parents[2]
MASTER_KEY = "sk-router-proof"
PORT = 4000
DB_PORT = 55433
POSTGRES_IMAGE = json.loads(
    (ROOT / "deploy/compose/images.lock.json").read_text(encoding="utf-8")
)["images"]["postgres"]
DB_PASSWORD = "router-proof"
ALIAS = "proof-pool"
# The marker that tells a client request from a gateway health probe.
REQUEST_MARKER = "router-proof-request"
SHUFFLE_REQUESTS = 20
BOUNDED_ATTEMPTS = 40
DEADLINE_SECONDS = 120


class Behaviour(enum.Enum):
    """How a controlled upstream answers."""

    OK = "ok"
    SERVER_ERROR = "server_error"
    UNAUTHORIZED = "unauthorized"


class Member:
    """One controlled upstream: its behaviour and the client requests it saw."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.behaviour = Behaviour.OK
        self.lock = threading.Lock()
        self.requests = 0
        self.server: ThreadingHTTPServer | None = None

    def count(self) -> int:
        with self.lock:
            return self.requests

    def served(self) -> None:
        with self.lock:
            self.requests += 1


class UpstreamHandler(BaseHTTPRequestHandler):
    member: Member

    def do_POST(self) -> None:
        raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        body = json.loads(raw)
        is_client = REQUEST_MARKER in json.dumps(body.get("messages", []))
        if is_client:
            self.member.served()
        if self.member.behaviour is Behaviour.SERVER_ERROR:
            self.reply(500, {"error": {"message": "stub failure", "type": "server"}})
        elif self.member.behaviour is Behaviour.UNAUTHORIZED:
            self.reply(401, {"error": {"message": "stub refusal", "type": "auth"}})
        elif body.get("stream"):
            self.stream()
        else:
            self.reply(200, self.completion(body))

    def completion(self, body: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": f"chatcmpl-{self.member.name}",
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
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }

    def reply(self, status: int, document: dict[str, Any]) -> None:
        answer = json.dumps(document).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(answer)))
        self.end_headers()
        self.wfile.write(answer)

    def stream(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        for index, text in enumerate(("one ", "two ", "three")):
            chunk = {
                "id": f"chatcmpl-{self.member.name}",
                "object": "chat.completion.chunk",
                "created": 0,
                "model": "stub",
                "choices": [
                    {
                        "index": 0,
                        "delta": {"role": "assistant", "content": text},
                        "finish_reason": "stop" if index == 2 else None,
                    }
                ],
            }
            self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
            self.wfile.flush()
        self.wfile.write(b"data: [DONE]\n\n")

    def log_message(self, format: str, *args: object) -> None:
        return


def start_member(name: str, bind: str) -> Member:
    member = Member(name)
    handler = type("Handler", (UpstreamHandler,), {"member": member})
    member.server = ThreadingHTTPServer((bind, 0), handler)
    threading.Thread(target=member.server.serve_forever, daemon=True).start()
    return member


def port_of(member: Member) -> int:
    assert member.server is not None
    return int(member.server.server_address[1])


def render(members: list[Member], host: str, with_health: bool) -> bytes:
    """The Controller's rendering for one route, widened to one entry per member."""
    models = {ALIAS: RouteAcceptedModelPolicy(upstream_model="pool")}
    rendered = json.loads(
        render_config(
            RouteState({ALIAS: f"http://{host}:{port_of(members[0])}/v1"}, "0" * 64),
            LiteLlmPolicy(models),
        )
    )
    template = rendered["model_list"][0]
    rendered["model_list"] = []
    for member in members:
        entry = json.loads(json.dumps(template))
        entry["litellm_params"]["api_base"] = f"http://{host}:{port_of(member)}/v1"
        entry["litellm_params"]["weight"] = 1
        entry["litellm_params"]["num_retries"] = 0
        entry["model_info"] = {"id": f"{member.name}:{'0' * 12}"}
        rendered["model_list"].append(entry)
    router = rendered.setdefault("router_settings", {})
    router["enable_health_check_routing"] = True
    router["health_check_ignore_transient_errors"] = True
    if with_health:
        router["background_health_check_model_groups"] = [ALIAS]
        general = rendered.setdefault("general_settings", {})
        general["background_health_checks"] = True
        general["health_check_interval"] = 5
    return (json.dumps(rendered, sort_keys=True) + "\n").encode()


def call(
    path: str, body: dict[str, object] | None = None, key: str = MASTER_KEY
) -> Any:
    request = urllib.request.Request(
        f"http://127.0.0.1:{PORT}{path}",
        data=None if body is None else json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode(errors="replace")


def check(condition: bool, message: str) -> None:
    if not condition:
        raise SystemExit(f"router proof failed: {message}")


def wait_until(description: str, condition: Callable[[], bool]) -> None:
    """Poll until the condition holds; the deadline bounds every wait."""
    deadline = time.monotonic() + DEADLINE_SECONDS
    while not condition():
        check(time.monotonic() < deadline, f"timed out waiting for {description}")
        time.sleep(1)


def wait_healthy() -> None:
    def alive() -> bool:
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{PORT}/health/liveliness", timeout=3
            ) as response:
                return response.status == 200
        except OSError:
            return False

    wait_until("LiteLLM to start with the rendered config", alive)


def generate_key(work: Path) -> str:
    """A client key created by the Controller's own key service."""
    service = GatewayKeyService(
        base_url=f"http://127.0.0.1:{PORT}",
        master_key=lambda: MASTER_KEY,
        intent_root=work / "keys",
    )
    deadline = time.monotonic() + DEADLINE_SECONDS
    while True:  # the key store is ready once the database migration finished
        created = service.create(f"router-proof-{time.monotonic_ns()}", models=[ALIAS])
        if isinstance(created, GatewayKeyCreated):
            return created.key
        check(time.monotonic() < deadline, "the key service could not create a key")
        time.sleep(2)


def sdk(key: str) -> openai.OpenAI:
    return openai.OpenAI(
        base_url=f"http://127.0.0.1:{PORT}/v1", api_key=key, max_retries=0, timeout=30
    )


def ask(client: openai.OpenAI) -> int:
    """One client request; the status the gateway answered with."""
    try:
        client.chat.completions.create(
            model=ALIAS,
            messages=[{"role": "user", "content": REQUEST_MARKER}],
            max_tokens=4,
        )
    except openai.APIStatusError as error:
        return error.status_code
    return 200


def counts(members: list[Member]) -> list[int]:
    return [member.count() for member in members]


def prove_without_health(client: openai.OpenAI, a: Member, b: Member) -> None:
    # Missing health: no sample exists, every member is eligible and both serve.
    for _ in range(SHUFFLE_REQUESTS):
        check(ask(client) == 200, "a healthy pool refused a request")
    check(
        a.count() > 0 and b.count() > 0 and a.count() + b.count() == SHUFFLE_REQUESTS,
        f"simple-shuffle served {counts([a, b])} of {SHUFFLE_REQUESTS}",
    )

    # One stream stays on one member from first chunk to last.
    before = counts([a, b])
    stream = client.chat.completions.create(
        model=ALIAS,
        messages=[{"role": "user", "content": REQUEST_MARKER}],
        stream=True,
    )
    chunks = list(stream)
    check(len(chunks) >= 3, f"the stream gave {len(chunks)} chunks")
    check(
        len({chunk.id for chunk in chunks}) == 1,
        "one stream carried chunks from two members",
    )
    served = [now - was for now, was in zip(counts([a, b]), before, strict=True)]
    check(sorted(served) == [0, 1], f"one stream reached members {served}")

    # num_retries 0: a failing member's error reaches the client, and no request
    # is sent to a second member after it.
    b.behaviour = Behaviour.SERVER_ERROR
    reached_failing = 0
    for _ in range(BOUNDED_ATTEMPTS):
        before = counts([a, b])
        status = ask(client)
        served = [now - was for now, was in zip(counts([a, b]), before, strict=True)]
        check(sum(served) <= 1, f"one request reached two members: {served}")
        if served[1] == 1:
            reached_failing += 1
            check(status != 200, "a failed member's request was retried elsewhere")
        if reached_failing >= 2:
            break
    check(reached_failing >= 1, "the failing member was never selected")
    b.behaviour = Behaviour.OK


def unhealthy_count() -> int:
    status, health = call("/health")
    return int(health["unhealthy_count"]) if status == 200 else -1


def healthy_count() -> int:
    status, health = call("/health")
    return int(health["healthy_count"]) if status == 200 else -1


def prove_with_health(client: openai.OpenAI, a: Member, b: Member) -> None:
    wait_until("both members to report healthy", lambda: healthy_count() == 2)

    # Every member unhealthy: the sample says so. What the gateway then does is
    # recorded, not assumed. TODO: pin the value the first batch CI run prints
    # here and in docs/architecture-overview.md.
    for member in (a, b):
        member.behaviour = Behaviour.UNAUTHORIZED
    wait_until("both members to report unhealthy", lambda: unhealthy_count() == 2)
    before = counts([a, b])
    status = ask(client)
    reached = [now - was for now, was in zip(counts([a, b]), before, strict=True)]
    check(max(reached) <= 1, f"one request reached a member twice: {reached}")
    print(
        f"ALL_UNHEALTHY_OUTCOME: status={status} members_reached={sum(reached)}",
        flush=True,
    )

    # Failures end in cooldown exhaustion: an error that never reached a member.
    exhausted: int | None = None
    for _ in range(BOUNDED_ATTEMPTS):
        before = sum(counts([a, b]))
        status = ask(client)
        if sum(counts([a, b])) == before:
            exhausted = status
            break
    check(exhausted is not None, "cooldown exhaustion was never reached")
    check(exhausted != 200, "cooldown exhaustion answered a success")
    print(f"cooldown exhaustion status: {exhausted}", flush=True)

    # Members answer again: the group recovers and serves both, unaided.
    for member in (a, b):
        member.behaviour = Behaviour.OK
    wait_until("the group to answer again", lambda: ask(client) == 200)
    wait_until("both members to report healthy again", lambda: healthy_count() == 2)
    before = counts([a, b])
    for _ in range(SHUFFLE_REQUESTS):
        check(ask(client) == 200, "a recovered pool refused a request")
    gained = [now - was for now, was in zip(counts([a, b]), before, strict=True)]
    check(all(gain > 0 for gain in gained), f"the recovered pool served {gained}")


def run_gateway(
    arguments: argparse.Namespace, name: str, config: Path, prove: Callable[[], None]
) -> None:
    container = f"vonk-router-proof-{name}"
    subprocess.run(["docker", "rm", "-f", container], check=False, capture_output=True)
    host = "host.docker.internal" if arguments.published else "127.0.0.1"
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
            f"LITELLM_DATABASE_URL=postgresql://postgres:{DB_PASSWORD}@{host}:{DB_PORT}/litellm",
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
        wait_healthy()
        prove()
    except BaseException:
        subprocess.run(["docker", "logs", "--tail", "200", container], check=False)
        raise
    finally:
        subprocess.run(
            ["docker", "rm", "-f", container], check=False, capture_output=True
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

    bind = "0.0.0.0" if arguments.published else "127.0.0.1"
    host = "host.docker.internal" if arguments.published else "127.0.0.1"
    a, b = start_member("member-a", bind), start_member("member-b", bind)
    arguments.work.mkdir(parents=True, exist_ok=True)
    database = "vonk-router-proof-db"
    subprocess.run(["docker", "rm", "-f", database], check=False, capture_output=True)
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
    try:
        for name, with_health, prove in (
            ("plain", False, prove_without_health),
            ("health", True, prove_with_health),
        ):
            config = arguments.work / f"litellm-{name}.json"
            config.write_bytes(render([a, b], host, with_health))
            config.chmod(0o644)
            run_gateway(
                arguments,
                name,
                config,
                lambda prove=prove: prove(sdk(generate_key(arguments.work)), a, b),
            )
    finally:
        subprocess.run(
            ["docker", "rm", "-f", database], check=False, capture_output=True
        )
        for member in (a, b):
            assert member.server is not None
            member.server.shutdown()
    print("router proof passed")


if __name__ == "__main__":
    sys.exit(main())
