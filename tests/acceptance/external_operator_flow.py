"""The operator's external-model flow on a freshly installed NAS bundle.

Driven through the deployed Controller API (an administrator token minted inside
``control-api``, as ``verify_deployed_controller_identity`` does) against the real
PostgreSQL, the real maintenance pass, the real supervisor and LiteLLM:

1. approve ``PARTNER``'s destination and add the model, each previewed and then
   accepted with the review's digest;
2. wait until the Controller reports it published;
3. create a client key at ``POST /api/key`` (no model list) and call the external
   alias with it, inside the LiteLLM container, and list it at ``/v1/models``;
4. repeated reads change nothing, and no Spark reservation exists;
5. remove the model, wait until it is gone, and see the alias refused while the
   partner received nothing more.

Acceptance-only trust: a throwaway CA, mounted into the gateway container by a
Compose overlay that sets ``SSL_CERT_FILE`` (certificate checks stay on), and a
partner stub that runs inside the gateway container under a dotted name resolved to itself. The secret is a file in
NAS custody, written before the stack starts. No Spark is enrolled, so there is no
local alias on this NAS: the local-model-keeps-serving leg is proven against the
built image by ``litellm_external_proof.py``.
"""

from __future__ import annotations

import json
import subprocess
import time
from collections.abc import Callable, Sequence
from pathlib import Path

from tests.acceptance.runtime import AcceptanceError

PARTNER_HOST = "partner.test.example"
PARTNER_PORT = 8443
ALIAS = "acceptance-partner"
SECRET = "acceptance-partner-secret"
PARTNER_LOG_SCRIPT = "import os; p = '/tmp/partner.log'; print(open(p).read() if os.path.exists(p) else '')"

PARTNER_STUB = """
import json, ssl
EXPECTED = "Bearer " + open("/acceptance/expected").read().strip()
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", "0")))
        with open("/tmp/partner.log", "a") as log:
            log.write("REQUEST " + str(self.headers.get("Authorization") == EXPECTED) + chr(10))
        answer = json.dumps({
            "id": "chatcmpl-acceptance", "object": "chat.completion", "created": 0,
            "model": "partner-large",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(answer)))
        self.end_headers()
        self.wfile.write(answer)

    def log_message(self, *args):
        return

context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
context.load_cert_chain("/acceptance/leaf.pem", "/acceptance/leaf.key")
server = ThreadingHTTPServer(("127.0.0.1", 8443), Handler)
server.socket = context.wrap_socket(server.socket, server_side=True)
with open("/tmp/partner.log", "a") as log:
    log.write("listening" + chr(10))
server.serve_forever()
"""

CONTROLLER_SCRIPT = """
import json, sys, time, urllib.error, urllib.request, uuid
from vonk_control.auth import Actor, TokenCodec
from vonk_control.settings import Settings

plan = json.load(sys.stdin)
token = TokenCodec(Settings.from_env_and_secrets().token_signing_key).issue(
    Actor("acceptance-operator", "administrator"), ttl_seconds=600, now=int(time.time()))

def call(method, path, body=None):
    request = urllib.request.Request(
        "http://127.0.0.1:8000" + path, method=method,
        data=None if body is None else json.dumps(body).encode(),
        headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, json.loads(response.read() or b"null")
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode()[:300]

def change(path, request):
    status, review = call("POST", path + "/preview", request)
    assert status == 200 and not review.get("problems"), (path, status, review)
    status, accepted = call("POST", path + "/accept", {
        **request, "request_id": str(uuid.uuid4()), "effect_digest": review["effect_digest"]})
    assert status == 200, (path, status, accepted)

def wait(check, label):
    deadline = time.monotonic() + 420
    while time.monotonic() < deadline:
        if check():
            return
        time.sleep(3)
    raise SystemExit("timed out waiting for " + label)

def model():
    status, view = call("GET", "/api/gateway/models/" + plan["alias"])
    return view if status == 200 else None

result = {}
if plan["step"] == "add":
    change("/api/gateway/credentials/PARTNER",
           {"action": "put", "destinations": [{"origin": plan["origin"]}]})
    change("/api/gateway/models/" + plan["alias"], {"action": "put", "entry": plan["entry"]})
    wait(lambda: (model() or {}).get("published") is True, "the model to be published")
    first = call("GET", "/api/gateway/models")
    second = call("GET", "/api/gateway/models")
    assert first == second, "a repeated read changed the answer"
    assert plan["secret"] not in json.dumps([first, second]), "a secret is in a listing"
    status, created = call("POST", "/api/key", {
        "name": "acceptance-client", "models": [], "request_id": str(uuid.uuid4())})
    assert status == 200, ("key", status)
    result["key"] = created["key"]
else:
    view = model()
    change("/api/gateway/models/" + plan["alias"],
           {"action": "remove", "expected_revision": view["desired_revision"]})
    wait(lambda: model() is None, "the model to be removed")
print(json.dumps(result))
"""

GATEWAY_SCRIPT = """
import json, sys, urllib.error, urllib.request

request = json.load(sys.stdin)

def call(method, path, body=None):
    http = urllib.request.Request(
        "http://127.0.0.1:4000" + path, method=method,
        data=None if body is None else json.dumps(body).encode(),
        headers={"Authorization": "Bearer " + request["key"], "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(http, timeout=60) as response:
            return response.status, response.read().decode()
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode()[:300]

listed_status, listed = call("GET", "/v1/models")
chat_status, _ = call("POST", "/v1/chat/completions", {
    "model": request["alias"], "messages": [{"role": "user", "content": "hi"}]})
print(json.dumps({"listed": listed_status == 200 and request["alias"] in listed,
                  "secret_listed": request["secret"] in listed, "chat": chat_status}))
"""


def _openssl(work: Path) -> None:
    """A throwaway CA and one certificate for the partner host."""
    extension = work / "leaf.ext"
    extension.write_text(f"subjectAltName=DNS:{PARTNER_HOST}\n")
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
            "/CN=vonk acceptance CA",
            "-keyout",
            "ca.key",
            "-out",
            "ca.pem",
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
            "leaf.key",
            "-out",
            "leaf.csr",
        ],
        [
            "openssl",
            "x509",
            "-req",
            "-in",
            "leaf.csr",
            "-CA",
            "ca.pem",
            "-CAkey",
            "ca.key",
            "-CAcreateserial",
            "-days",
            "2",
            "-extfile",
            "leaf.ext",
            "-out",
            "leaf.pem",
        ],
    ):
        subprocess.run(command, cwd=work, check=True, capture_output=True)
    for name in ("ca.pem", "leaf.pem", "leaf.key"):
        (work / name).chmod(0o644)


def prepare(bundle: Path, work: Path) -> Path:
    """Before the stack starts: the secret in custody, the CA, and the gateway overlay."""
    work.mkdir(parents=True, exist_ok=True)
    _openssl(work)
    (work / "expected").write_text(SECRET + "\n")
    custody = bundle / "secrets" / "gateway-credentials"
    custody.mkdir(parents=True, exist_ok=True)
    (custody / "PARTNER").write_text(SECRET + "\n")
    (custody / "PARTNER").chmod(0o600)
    overlay = work / "trust-overlay.yml"
    overlay.write_text(
        "\n".join(
            [
                "services:",
                "  litellm:",
                "    environment:",
                "      SSL_CERT_FILE: /acceptance/ca.pem",
                "    extra_hosts:",
                f"      - {PARTNER_HOST}:127.0.0.1",
                "    volumes:",
                f"      - {work}:/acceptance:ro",
                "",
            ]
        )
    )
    return overlay


def _text(command: Sequence[str], *, cwd: Path, stdin: str | None = None) -> str:
    result = subprocess.run(
        command,
        cwd=cwd,
        input=stdin,
        text=True,
        capture_output=True,
        timeout=600,
        check=False,
    )
    if result.returncode != 0:
        # The command line and stdin (which may carry a key) are never echoed.
        raise AcceptanceError(
            f"external flow step failed ({command[0]}):\n{result.stdout[-1500:]}\n"
            f"{result.stderr[-2500:]}"
        )
    return result.stdout


def _start_partner(compose: Sequence[str], bundle: Path) -> None:
    """The partner runs inside the gateway container, which resolves its name to itself."""
    _text(
        [*compose, "exec", "-d", "-T", "litellm", "python", "-c", PARTNER_STUB],
        cwd=bundle,
    )
    deadline = time.monotonic() + 30
    while "listening" not in _partner_log(compose, bundle):
        if time.monotonic() > deadline:
            raise AcceptanceError("the partner stub did not start")
        time.sleep(1)


def _partner_log(compose: Sequence[str], bundle: Path) -> str:
    return _text(
        [*compose, "exec", "-T", "litellm", "python", "-c", PARTNER_LOG_SCRIPT],
        cwd=bundle,
    )


def _partner_requests(compose: Sequence[str], bundle: Path) -> tuple[int, int]:
    """How many requests the partner saw, and how many carried the bound credential."""
    lines = [
        line
        for line in _partner_log(compose, bundle).splitlines()
        if line.startswith("REQUEST")
    ]
    return len(lines), sum(line.endswith("True") for line in lines)


def _gateway(compose: Sequence[str], bundle: Path, key: str) -> dict[str, object]:
    return json.loads(
        _text(
            [*compose, "exec", "-T", "litellm", "python", "-c", GATEWAY_SCRIPT],
            cwd=bundle,
            stdin=json.dumps({"key": key, "alias": ALIAS, "secret": SECRET}),
        )
    )


def exercise(compose: Callable[[], Sequence[str]], bundle: Path, work: Path) -> None:
    """After the stack is healthy: add, publish, call with a client key, remove."""
    command = list(compose())
    _start_partner(command, bundle)
    base = f"https://{PARTNER_HOST}:{PARTNER_PORT}"
    entry = {
        "model_name": ALIAS,
        "litellm_params": {
            "model": "openai/partner-large",
            "api_base": f"{base}/v1",
            "api_key": "os.environ/VONK_CREDENTIAL_PARTNER",
        },
    }
    controller = [
        *command,
        "exec",
        "-T",
        "control-api",
        "python",
        "-c",
        CONTROLLER_SCRIPT,
    ]
    added = json.loads(
        _text(
            controller,
            cwd=bundle,
            stdin=json.dumps(
                {
                    "step": "add",
                    "alias": ALIAS,
                    "origin": base,
                    "entry": entry,
                    "secret": SECRET,
                }
            ),
        )
        .strip()
        .splitlines()[-1]
    )
    key = added["key"]
    reservations = _text(
        [
            *command,
            "exec",
            "-T",
            "postgres",
            "psql",
            "-U",
            "control",
            "-d",
            "control",
            "-Atc",
            "SELECT count(*) FROM resource_reservations",
        ],
        cwd=bundle,
    ).strip()
    if reservations != "0":
        raise AcceptanceError("an external model made a Spark reservation")
    served = _gateway(command, bundle, key)
    seen, authorised = _partner_requests(command, bundle)
    if served != {"listed": True, "secret_listed": False, "chat": 200}:
        raise AcceptanceError(
            f"the client key did not reach the external model: {served}"
        )
    if seen < 1 or authorised != seen:
        raise AcceptanceError("the partner did not receive the bound credential")

    _text(
        controller,
        cwd=bundle,
        stdin=json.dumps({"step": "remove", "alias": ALIAS, "secret": SECRET}),
    )
    deadline = time.monotonic() + 420
    while (
        True
    ):  # the gateway reloads once the generation without the entry is acknowledged
        gone = _gateway(command, bundle, key)
        if gone["chat"] != 200 and gone["listed"] is False:
            break
        if time.monotonic() > deadline:
            raise AcceptanceError("the removed alias still answers")
        time.sleep(5)
    if _partner_requests(command, bundle)[0] != seen:
        raise AcceptanceError("a request reached the partner after removal")
