"""Prove an external model through the real LiteLLM image, with a client's credential.

One client virtual key, created by the real ``GatewayKeyService`` with no model
list (the path the API takes), reaches a local model and an external model with
streaming. Two controlled HTTPS stubs stand in for the outside world: "partner",
an OpenAI-compatible upstream behind a custom ``api_base`` that checks the credential
it is sent, and "elsewhere", which only records what reaches it. A test CA, given to
the gateway container alone, makes both certificates valid; TLS verification stays on.

The proof asserts what that client sees and what the gateway does:

* the key streams a reply from the local alias and from the external alias, and the
  partner received the bound credential;
* ``/v1/models`` and ``/v1/model/info`` list both aliases (and a second external
  adapter, ``vertex_ai/``, which loads and sends nothing) and no response holds a
  resolved secret;
* a 307 redirect from the partner to "elsewhere" leaves "elsewhere" with no request
  that carries a credential;
* after a generation without the external entries is published and the supervisor
  reloads the gateway,
  the next call to the alias fails and the partner received nothing more.

It runs against the built image (the batch CI job) through the real entrypoint and
supervisor, as Compose starts them: the secrets are staged files that only the
supervisor reads and exports into the LiteLLM child, ``credentials.json`` names them
without a value, and removal is a published generation the supervisor loads.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import shutil
import ssl
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import ClassVar

import openai
from fastapi import HTTPException
from litellm_model_info_proof import (
    DB_PASSWORD,
    DB_PORT,
    MASTER_KEY,
    PORT,
    POSTGRES_IMAGE,
    ROOT,
    call,
    check,
    sdk,
    wait_healthy,
)

from vonk_agent_protocol.route_activation import SupervisorPhase
from vonk_agent_protocol.state_machines import GatewayRouteState
from vonk_control.gateway.external_contract import (
    ExternalDeploymentSpec,
    ExternalDestination,
)
from vonk_control.gateway.keys import GatewayKeyError, GatewayKeyService
from vonk_control.gateway.keys_contract import GatewayKeyCreated
from vonk_control.gateway.litellm import (
    LiteLlmPolicy,
    RouteState,
    render_config,
    render_external_entry,
)
from vonk_control.gateway.route_bundle_contract import RouteAcceptedModelPolicy
from vonk_control.gateway.route_runtime import AtomicRouteBundlePublisher
from vonk_control.recipe_routes.publisher import AtomicRecipeRoutePublisher

SECRET = "partner-secret-value"
PARTNER_HOST = "partner.test.example"
ELSEWHERE_HOST = "elsewhere.test.example"
LOCAL = "proof-local"
EXTERNAL = "proof-partner"
REDIRECTED = "proof-redirected"
VERTEX = "proof-vertex"
REPLY = "ok"


class Recording(BaseHTTPRequestHandler):
    """A chat upstream that records every request; it streams, and can redirect."""

    received: ClassVar[list[dict[str, str]]]
    redirect_to: ClassVar[str | None] = None

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length) or b"{}")
        self.received.append(
            {"path": self.path, "authorization": self.headers.get("Authorization", "")}
        )
        if self.path.startswith("/redirect/") and self.redirect_to:
            self.send_response(307)
            self.send_header("Location", f"{self.redirect_to}{self.path}")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if body.get("stream"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for piece in (REPLY[:1], REPLY[1:]):
                chunk = {
                    "id": "chatcmpl-proof",
                    "object": "chat.completion.chunk",
                    "created": 0,
                    "model": body.get("model", ""),
                    "choices": [
                        {"index": 0, "delta": {"content": piece}, "finish_reason": None}
                    ],
                }
                self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
            self.wfile.write(b"data: [DONE]\n\n")
            return
        answer = json.dumps(
            {
                "id": "chatcmpl-proof",
                "object": "chat.completion",
                "created": 0,
                "model": body.get("model", ""),
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": REPLY},
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

    def log_message(self, format: str, *args: object) -> None:
        return


class Local(Recording):
    received: ClassVar[list[dict[str, str]]] = []


class Partner(Recording):
    received: ClassVar[list[dict[str, str]]] = []


class Elsewhere(Recording):
    received: ClassVar[list[dict[str, str]]] = []


def serve(
    handler: type[BaseHTTPRequestHandler], tls: ssl.SSLContext | None = None
) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("0.0.0.0", 0), handler)
    if tls is not None:
        server.socket = tls.wrap_socket(server.socket, server_side=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def make_authority(work: Path) -> tuple[Path, ssl.SSLContext]:
    """A throwaway CA and one certificate naming both stub hosts."""
    ca_key, ca_pem = work / "ca.key", work / "ca.pem"
    key, request, certificate = work / "leaf.key", work / "leaf.csr", work / "leaf.pem"
    extension = work / "leaf.ext"
    extension.write_text(f"subjectAltName=DNS:{PARTNER_HOST},DNS:{ELSEWHERE_HOST}\n")
    for command in (
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-days",
            "2",
            "-subj",
            "/CN=vonk proof CA",
            "-keyout",
            str(ca_key),
            "-out",
            str(ca_pem),
        ],
        [
            "openssl",
            "req",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-subj",
            f"/CN={PARTNER_HOST}",
            "-keyout",
            str(key),
            "-out",
            str(request),
        ],
        [
            "openssl",
            "x509",
            "-req",
            "-in",
            str(request),
            "-CA",
            str(ca_pem),
            "-CAkey",
            str(ca_key),
            "-CAcreateserial",
            "-days",
            "2",
            "-extfile",
            str(extension),
            "-out",
            str(certificate),
        ],
    ):
        subprocess.run(command, check=True, capture_output=True)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certificate, key)
    ca_pem.chmod(0o644)
    return ca_pem, context


def specs(partner_port: int) -> list[ExternalDeploymentSpec]:
    base = f"https://{PARTNER_HOST}:{partner_port}"
    destination = ExternalDestination(origins=[base])
    secret = "os.environ/VONK_CREDENTIAL_PARTNER"
    return [
        ExternalDeploymentSpec(
            alias=EXTERNAL,
            litellm_params={
                "model": "openai/partner-large",
                "api_base": f"{base}/v1",
                "api_key": secret,
            },
            destination=destination,
            credential_bindings=["PARTNER"],
        ),
        ExternalDeploymentSpec(
            alias=REDIRECTED,
            litellm_params={
                "model": "openai/partner-redirect",
                "api_base": f"{base}/redirect/v1",
                "api_key": secret,
            },
            destination=destination,
            credential_bindings=["PARTNER"],
        ),
        ExternalDeploymentSpec(
            alias=VERTEX,
            litellm_params={
                "model": "vertex_ai/gemini-2.5-pro",
                "vertex_project": "proof",
                "vertex_location": "europe-west4",
                "vertex_credentials": "os.environ/VONK_CREDENTIAL_VERTEX_SA",
            },
            destination=ExternalDestination(
                origins=["provider:vertex_ai"], region="europe-west4"
            ),
            credential_bindings=["VERTEX_SA"],
        ),
    ]


def render(local_port: int, partner_port: int | None) -> bytes:
    """The config the publisher renders: one local route and, while published, the externals."""
    policy = LiteLlmPolicy(
        {LOCAL: RouteAcceptedModelPolicy(upstream_model="local-model")}
    )
    entries = (
        []
        if partner_port is None
        else [
            render_external_entry(
                spec, f"0d4f5b0e-0000-4000-8000-0000000000{index:02d}", 1
            )
            for index, spec in enumerate(specs(partner_port))
        ]
    )
    return render_config(
        RouteState({LOCAL: f"http://127.0.0.1:{local_port}/v1"}, "0" * 64),
        policy,
        entries,
    )


def client_key(work: Path) -> str:
    """The key the API creates for a client: no model list, so every model."""
    service = GatewayKeyService(
        base_url=f"http://127.0.0.1:{PORT}",
        master_key=lambda: MASTER_KEY,
        intent_root=work / "mutations",
    )
    deadline = time.monotonic() + 120
    while True:  # the key store is ready once the database migration finished
        try:
            created = service.create(
                "proof-client", request_id="proof-request", actor="proof"
            )
            if isinstance(created, GatewayKeyCreated):
                return created.key
        except (HTTPException, GatewayKeyError):
            pass  # LiteLLM answers the key routes only after its migration
        check(time.monotonic() < deadline, "the client key was not created")
        time.sleep(2)


def streamed(key: str, alias: str) -> str:
    stream = sdk(key).chat.completions.create(
        model=alias, messages=[{"role": "user", "content": "hi"}], stream=True
    )
    return "".join(
        chunk.choices[0].delta.content or "" for chunk in stream if chunk.choices
    )


def prove(key: str, elsewhere_port: int, remove: Callable[[], None]) -> None:
    check(
        streamed(key, LOCAL) == REPLY,
        "the local model did not stream for the client key",
    )
    check(
        streamed(key, EXTERNAL) == REPLY,
        "the external model did not stream for the client key",
    )
    check(
        any(item["authorization"] == f"Bearer {SECRET}" for item in Partner.received),
        "the partner did not receive the bound credential",
    )

    # Discovery with the client's own key lists every alias and no secret.
    status, models = call("/v1/models", key=key)
    check(status == 200, f"/v1/models answered {status} for the client key")
    status, info = call("/v1/model/info", key=key)
    check(status == 200, f"/v1/model/info answered {status} for the client key")
    for alias in (LOCAL, EXTERNAL, VERTEX):
        check(alias in json.dumps(models), f"{alias} is not in /v1/models")
        check(alias in json.dumps(info), f"{alias} is not in /v1/model/info")
    for answer in (models, info):
        check(SECRET not in json.dumps(answer), "a resolved secret is in discovery")

    # A redirect to another host carries no credential.
    Partner.redirect_to = f"https://{ELSEWHERE_HOST}:{elsewhere_port}"
    # The gateway may refuse the redirect; what matters is what leaked, and that
    # the partner was asked (otherwise nothing here was tested).
    with contextlib.suppress(openai.APIStatusError):
        streamed(key, REDIRECTED)
    check(
        any(item["path"].startswith("/redirect/") for item in Partner.received),
        "the redirecting partner was never asked",
    )
    check(
        not any(SECRET in item["authorization"] for item in Elsewhere.received),
        "a redirect carried the credential to another host",
    )

    # After removal and a reload, the alias is gone and nothing more is sent upstream.
    seen = len(Partner.received)
    remove()
    refused = False
    try:
        streamed(key, EXTERNAL)
    except openai.APIStatusError as error:
        refused = error.status_code >= 400
    check(refused, "the removed external alias still answered")
    check(len(Partner.received) == seen, "a request reached the partner after removal")
    check(
        streamed(key, LOCAL) == REPLY, "the local model stopped serving after removal"
    )


def publish(routes: Path, config: bytes, label: str) -> None:
    """Publish one generation as the Controller does, for the real supervisor to load."""
    publisher = AtomicRecipeRoutePublisher(AtomicRouteBundlePublisher(routes))
    publisher._activate(  # the proof publishes the exact bundle the Controller writes
        hashlib.sha256(label.encode()).hexdigest(),
        config,
        endpoints={},
        state=GatewayRouteState.PUBLISHED,
        required=SupervisorPhase.READY,
    )
    # The supervisor runs as another user: it reads the bundle, never writes it.
    subprocess.run(["chmod", "-R", "a+rX", str(routes)], check=True)


def wait_acknowledged(supervisor: Path, generation: int) -> None:
    """Wait for the supervisor to say a healthy child serves this generation."""
    deadline = time.monotonic() + 240
    while time.monotonic() < deadline:
        with contextlib.suppress(OSError, ValueError):
            document = json.loads((supervisor / "ack.json").read_text())
            if document.get("generation") == generation and "phase" not in document:
                return
        time.sleep(2)
    raise SystemExit(f"the supervisor never acknowledged generation {generation}")


def stage_custody(work: Path) -> tuple[Path, Path, Path, Path]:
    """What runtime-init stages: the kit, the staged secrets and the shared state."""
    kit = work / "kit" / "litellm"
    kit.mkdir(parents=True)
    for name in ("entrypoint.sh", "config_supervisor.py", "bootstrap-config.json"):
        shutil.copyfile(ROOT / "deploy/compose/litellm" / name, kit / name)
    secrets = work / "secrets"
    credentials = secrets / "gateway-credentials"
    credentials.mkdir(parents=True)
    files = {
        secrets / "litellm-master-key": MASTER_KEY,
        secrets / "litellm-upstream-key": "stub-upstream-key",
        secrets / "litellm-database-url": (
            f"postgresql://postgres:{DB_PASSWORD}@127.0.0.1:{DB_PORT}/litellm"
        ),
        credentials / "PARTNER": SECRET,
        credentials / "VERTEX_SA": '{"type": "service_account"}',
    }
    for path, value in files.items():
        path.write_text(value + "\n")
    supervisor = work / "supervisor"
    supervisor.mkdir()
    supervisor.chmod(0o777)
    routes = work / "routes"
    subprocess.run(["chmod", "-R", "a+rX", str(work / "kit"), str(secrets)], check=True)
    return kit, secrets, supervisor, routes


def prove_custody(supervisor: Path) -> None:
    """The supervisor exported the staged secrets and told the Controller only their names."""
    deadline = time.monotonic() + 60
    path = supervisor / "credentials.json"
    while not path.is_file():
        check(time.monotonic() < deadline, "the supervisor wrote no credentials.json")
        time.sleep(1)
    text = path.read_text()
    document = json.loads(text)
    check(document["names"] == ["PARTNER", "VERTEX_SA"], f"names are {document}")
    check(isinstance(document["child_pid"], int), "no child pid")
    check(SECRET not in text and "service_account" not in text, "a value is in it")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    parser.add_argument("--work", type=Path, required=True)
    arguments = parser.parse_args()
    arguments.work.mkdir(parents=True, exist_ok=True)

    ca_pem, tls = make_authority(arguments.work)
    local, partner, elsewhere = serve(Local), serve(Partner, tls), serve(Elsewhere, tls)
    kit, secrets, supervisor, routes = stage_custody(arguments.work)
    publish(
        routes,
        render(local.server_address[1], partner.server_address[1]),
        "with-externals",
    )
    container, database = "vonk-external-proof", "vonk-external-proof-db"
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
    # The real entrypoint and supervisor, as Compose runs them: the secrets reach
    # the child only through the supervisor, from the staged custody directory.
    subprocess.run(
        [
            "docker",
            "run",
            "--detach",
            "--name",
            container,
            "--network",
            "host",
            "--add-host",
            f"{PARTNER_HOST}:127.0.0.1",
            "--add-host",
            f"{ELSEWHERE_HOST}:127.0.0.1",
            "--env",
            "HOME=/root",
            "--env",
            "XDG_CACHE_HOME=/root/.cache",
            "--env",
            "PRISMA_QUERY_ENGINE_BINARY=/opt/vonk-litellm/prisma/query-engine",
            "--env",
            "LITELLM_UI_PATH=/tmp/litellm-ui",
            "--env",
            "STORE_MODEL_IN_DB=False",
            "--env",
            "LITELLM_MASTER_KEY_FILE=/run/vonk-normalized-secrets/litellm-master-key",
            "--env",
            "LITELLM_UPSTREAM_KEY_FILE=/run/vonk-normalized-secrets/litellm-upstream-key",
            "--env",
            "LITELLM_DATABASE_URL_FILE=/run/vonk-normalized-secrets/litellm-database-url",
            "--env",
            "SSL_CERT_FILE=/proof/ca.pem",
            "--tmpfs",
            "/tmp",
            "--tmpfs",
            "/root:exec,mode=0700,uid=10002,gid=10001",
            "--volume",
            f"{kit}:/run/vonk-runtime-assets/litellm:ro",
            "--volume",
            f"{secrets}:/run/vonk-normalized-secrets:ro",
            "--volume",
            f"{routes}:/routes:ro",
            "--volume",
            f"{supervisor}:/supervisor:rw",
            "--volume",
            f"{ca_pem}:/proof/ca.pem:ro",
            "--entrypoint",
            "/bin/sh",
            arguments.image,
            "/run/vonk-runtime-assets/litellm/entrypoint.sh",
        ],
        check=True,
    )

    def remove() -> None:
        # What a published withdrawal does: a generation without the external
        # entries, which the supervisor loads by restarting the child.
        publish(routes, render(local.server_address[1], None), "without-externals")
        wait_acknowledged(supervisor, 2)

    try:
        wait_healthy(container)
        wait_acknowledged(supervisor, 1)
        prove_custody(supervisor)
        prove(client_key(arguments.work), elsewhere.server_address[1], remove)
    except BaseException:
        subprocess.run(["docker", "logs", "--tail", "200", container], check=False)
        raise
    finally:
        for name in (container, database):
            subprocess.run(
                ["docker", "rm", "-f", name], check=False, capture_output=True
            )
        for server in (local, partner, elsewhere):
            server.shutdown()
    print("external model proof passed")


if __name__ == "__main__":
    sys.exit(main())
