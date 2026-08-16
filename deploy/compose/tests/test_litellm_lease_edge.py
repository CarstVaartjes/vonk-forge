from __future__ import annotations

import importlib.util
import multiprocessing
import shutil
import socket
import subprocess
import sys
import time
import types
import urllib.error
import urllib.request
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
SUPERVISOR = ROOT / "deploy/compose/litellm/config_supervisor.py"
CADDY_IMAGE = (
    "caddy:2.11.4@sha256:"
    "844f60b64e4724a5aa8245e019dace0d3f199f7433ce6c57676cb30a920dbad9"
)
PRE_EXPIRY_ASSERTION_WINDOW = timedelta(seconds=5)
POST_RENEWAL_ASSERTION_WINDOW = timedelta(seconds=10)


def _supervisor_module():
    spec = importlib.util.spec_from_file_location(
        "litellm_config_supervisor_lease_edge", SUPERVISOR
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _serve_upstream(port_sender, request_count) -> None:
    class UpstreamHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self) -> None:
            with request_count.get_lock():
                request_count.value += 1
            body = b"real upstream\n"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, _format: str, *_args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), UpstreamHandler)
    server.daemon_threads = True
    port_sender.send(server.server_address[1])
    port_sender.close()
    server.serve_forever()


def _serve_authority(connection) -> None:
    module = _supervisor_module()
    authority = module._RouteLeaseAuthority()
    server = module._start_route_lease_server(
        authority,
        host="127.0.0.1",
        port=0,
    )
    connection.send(("port", server.server_address[1]))
    try:
        while True:
            command, payload = connection.recv()
            if command == "allow_bootstrap":
                authority.allow_bootstrap()
            elif command == "activate":
                authority.activate(payload)
            elif command == "stop":
                server.shutdown()
                server.server_close()
                connection.send(("ok", None))
                return
            else:
                raise RuntimeError(f"unknown authority test command: {command}")
            connection.send(("ok", None))
    finally:
        server.shutdown()
        server.server_close()
        connection.close()


def _unused_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def _edge_status(port: int) -> tuple[int, bytes]:
    request = urllib.request.Request(f"http://127.0.0.1:{port}/v1/models")
    try:
        response = urllib.request.urlopen(request, timeout=3)
    except urllib.error.HTTPError as error:
        return error.code, error.read()
    with response:
        return response.status, response.read()


def _wait_for_listener(process: subprocess.Popen[str], port: int) -> None:
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if process.poll() is not None:
            output, _ = process.communicate(timeout=1)
            pytest.fail(f"pinned Caddy exited during startup:\n{output[-16_384:]}")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return
        except OSError:
            time.sleep(0.05)
    pytest.fail("pinned Caddy did not open its listener within 15 seconds")


def _wait_until(deadline: datetime) -> None:
    while (remaining := (deadline - datetime.now(UTC)).total_seconds()) > 0:
        time.sleep(min(remaining, 0.01))


def _active_request(*, generation: int, expires_at: datetime) -> object:
    return types.SimpleNamespace(
        activation_sha256=f"{generation:064x}",
        marker={
            "generation": generation,
            "litellm_sha256": "a" * 64,
            "expires_at": expires_at.isoformat(),
        },
    )


@dataclass
class _AuthorityControl:
    connection: object
    process: multiprocessing.Process

    def _command(self, command: str, payload: object = None) -> None:
        self.connection.send((command, payload))
        if not self.connection.poll(5):
            raise AssertionError(f"authority did not acknowledge {command!r}")
        assert self.connection.recv() == ("ok", None)

    def allow_bootstrap(self) -> None:
        self._command("allow_bootstrap")

    def activate(self, request: object) -> None:
        self._command("activate", request)

    def stop(self) -> None:
        self._command("stop")
        self.process.join(timeout=5)
        assert not self.process.is_alive(), "authority process did not stop"


@dataclass
class _RealLeaseEdge:
    authority: _AuthorityControl
    caddy: subprocess.Popen[str]
    caddy_port: int
    container_name: str
    request_count: object
    upstream: multiprocessing.Process

    def stop_authority(self) -> None:
        self.authority.stop()


@pytest.fixture
def real_lease_edge(tmp_path: Path) -> Iterator[_RealLeaseEdge]:
    if shutil.which("docker") is None:
        pytest.skip("Docker CLI is unavailable")
    docker_info = subprocess.run(
        ["docker", "info", "--format", "{{.ServerVersion}}"],
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )
    if docker_info.returncode != 0:
        pytest.skip("Docker daemon is unavailable")

    context = multiprocessing.get_context("spawn")
    port_receiver, port_sender = context.Pipe(duplex=False)
    request_count = context.Value("I", 0)
    upstream = context.Process(
        target=_serve_upstream,
        args=(port_sender, request_count),
        daemon=True,
    )
    upstream.start()
    port_sender.close()
    if not port_receiver.poll(10):
        upstream.terminate()
        upstream.join(timeout=5)
        pytest.fail("real upstream process did not start within 10 seconds")
    upstream_port = port_receiver.recv()
    port_receiver.close()

    authority_parent, authority_child = context.Pipe()
    authority_process = context.Process(
        target=_serve_authority,
        args=(authority_child,),
        daemon=True,
    )
    authority_process.start()
    authority_child.close()
    if not authority_parent.poll(10):
        authority_process.terminate()
        authority_process.join(timeout=5)
        pytest.fail("production authority server did not start within 10 seconds")
    authority_message = authority_parent.recv()
    assert authority_message[0] == "port"
    authority_port = authority_message[1]
    authority = _AuthorityControl(authority_parent, authority_process)
    caddy_port = _unused_port()
    caddyfile = tmp_path / "Caddyfile"
    caddyfile.write_text(
        f"""{{
	admin off
	auto_https off
}}

(litellm_route_lease) {{
	forward_auth 127.0.0.1:{authority_port} {{
		uri /vonk/route-lease
	}}
}}

http://127.0.0.1:{caddy_port} {{
	import litellm_route_lease
	reverse_proxy 127.0.0.1:{upstream_port}
}}
""",
        encoding="utf-8",
    )
    container_name = f"vonk-lease-edge-{uuid.uuid4().hex[:12]}"
    caddy = subprocess.Popen(
        [
            "docker",
            "run",
            "--rm",
            "--name",
            container_name,
            "--network",
            "host",
            "--volume",
            f"{caddyfile}:/etc/caddy/Caddyfile:ro",
            CADDY_IMAGE,
            "caddy",
            "run",
            "--config",
            "/etc/caddy/Caddyfile",
            "--adapter",
            "caddyfile",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    edge = _RealLeaseEdge(
        authority=authority,
        caddy=caddy,
        caddy_port=caddy_port,
        container_name=container_name,
        request_count=request_count,
        upstream=upstream,
    )
    try:
        _wait_for_listener(caddy, caddy_port)
        yield edge
    finally:
        if authority_process.is_alive():
            try:
                authority.stop()
            except (AssertionError, BrokenPipeError, EOFError, OSError):
                authority_process.terminate()
                authority_process.join(timeout=5)
        authority_parent.close()
        subprocess.run(
            ["docker", "rm", "--force", container_name],
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
        try:
            caddy.wait(timeout=10)
        except subprocess.TimeoutExpired:
            caddy.kill()
            caddy.wait(timeout=5)
        if upstream.is_alive():
            upstream.terminate()
        upstream.join(timeout=5)
        if upstream.is_alive():
            upstream.kill()
            upstream.join(timeout=5)


def test_real_caddy_forwards_bootstrap_request_to_live_upstream(
    real_lease_edge: _RealLeaseEdge,
) -> None:
    real_lease_edge.authority.allow_bootstrap()

    assert _edge_status(real_lease_edge.caddy_port) == (200, b"real upstream\n")
    assert real_lease_edge.request_count.value == 1
    assert real_lease_edge.upstream.is_alive()


def test_real_caddy_denies_at_expiry_without_contacting_live_upstream(
    real_lease_edge: _RealLeaseEdge,
) -> None:
    expires_at = datetime.now(UTC) + PRE_EXPIRY_ASSERTION_WINDOW
    real_lease_edge.authority.activate(
        _active_request(generation=1, expires_at=expires_at)
    )
    assert _edge_status(real_lease_edge.caddy_port)[0] == 200
    admitted_count = real_lease_edge.request_count.value

    _wait_until(expires_at)
    assert _edge_status(real_lease_edge.caddy_port)[0] // 100 != 2
    assert real_lease_edge.request_count.value == admitted_count
    assert real_lease_edge.upstream.is_alive()


def test_real_caddy_fails_closed_when_authority_stops(
    real_lease_edge: _RealLeaseEdge,
) -> None:
    real_lease_edge.authority.allow_bootstrap()
    assert _edge_status(real_lease_edge.caddy_port)[0] == 200
    admitted_count = real_lease_edge.request_count.value

    real_lease_edge.stop_authority()
    assert _edge_status(real_lease_edge.caddy_port)[0] // 100 != 2
    assert real_lease_edge.request_count.value == admitted_count
    assert real_lease_edge.upstream.is_alive()


def test_real_caddy_honors_same_config_renewal_until_renewed_deadline(
    real_lease_edge: _RealLeaseEdge,
) -> None:
    now = datetime.now(UTC)
    old_deadline = now + PRE_EXPIRY_ASSERTION_WINDOW
    renewed_deadline = old_deadline + POST_RENEWAL_ASSERTION_WINDOW
    real_lease_edge.authority.activate(
        _active_request(generation=1, expires_at=old_deadline)
    )
    real_lease_edge.authority.activate(
        _active_request(generation=2, expires_at=renewed_deadline)
    )

    _wait_until(old_deadline)
    assert _edge_status(real_lease_edge.caddy_port)[0] == 200
    admitted_count = real_lease_edge.request_count.value

    _wait_until(renewed_deadline)
    assert _edge_status(real_lease_edge.caddy_port)[0] // 100 != 2
    assert real_lease_edge.request_count.value == admitted_count
    assert real_lease_edge.upstream.is_alive()
