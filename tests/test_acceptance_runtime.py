from __future__ import annotations

import json
import socket
import ssl
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from tests.acceptance.runtime import (
    AcceptanceError,
    assert_bundle_contract,
    assert_compose_compatibility,
    assert_compose_services_healthy,
    https_over_command,
    run_interactive,
    write_all,
)


def test_interactive_runner_drives_a_real_tty_without_exporting_answers(
    tmp_path: Path,
) -> None:
    child = tmp_path / "prompt.py"
    child.write_text(
        "import os\n"
        "with open('/dev/tty', 'w') as output, open('/dev/tty', 'r') as input:\n"
        "    output.write('Pairing token: ')\n"
        "    output.flush()\n"
        "    value = input.readline().strip()\n"
        "    assert value == 'token-value'\n"
        "    assert 'token-value' not in os.environ.values()\n"
    )

    transcript = run_interactive(
        [sys.executable, child],
        cwd=tmp_path,
        environment={"PATH": "/usr/bin:/bin"},
        responses=[("Pairing token: ", "token-value")],
        timeout=5,
    )

    assert "Pairing token: " in transcript


def test_interactive_runner_can_allow_upgrade_prompts_to_be_unchanged(
    tmp_path: Path,
) -> None:
    child = tmp_path / "prompt.py"
    child.write_text(
        "with open('/dev/tty', 'w') as output, open('/dev/tty', 'r') as input:\n"
        "    output.write('Existing value: ')\n"
        "    output.flush()\n"
        "    assert input.readline().strip() == 'keep'\n"
    )

    transcript = run_interactive(
        [sys.executable, child],
        cwd=tmp_path,
        environment={"PATH": "/usr/bin:/bin"},
        responses=[("Existing value: ", "keep"), ("Not prompted on upgrade: ", "")],
        timeout=5,
        require_all_prompts=False,
    )

    assert "Existing value: " in transcript


def test_bundle_contract_is_exact_and_contains_no_secret_values_in_compose(
    tmp_path: Path,
) -> None:
    bundle = tmp_path / "vonk-forge"
    secrets = bundle / "secrets"
    secrets.mkdir(parents=True)
    (bundle / "docker-compose.yaml").write_text(
        "services:\n  api:\n    secrets: [token]\nsecrets:\n  token:\n    file: ./secrets/token\n"
    )
    (bundle / ".env").write_text("COMPOSE_PROJECT_NAME=vonk-forge-control\n")
    (secrets / "token").write_text("do-not-embed\n")
    (bundle / "docker-compose.yaml").chmod(0o644)
    (bundle / ".env").chmod(0o600)
    secrets.chmod(0o700)
    (secrets / "token").chmod(0o600)

    assert_bundle_contract(bundle)

    (bundle / "README.md").write_text("extra")
    with pytest.raises(AcceptanceError, match="exactly"):
        assert_bundle_contract(bundle)


def test_compose_health_parser_rejects_exited_or_unhealthy_services() -> None:
    healthy = json.dumps(
        [
            {"Service": "api", "State": "running", "Health": "healthy"},
            {"Service": "db", "State": "running", "Health": "healthy"},
        ]
    )
    assert_compose_services_healthy(healthy, {"api", "db"})

    exited = json.dumps(
        [
            {"Service": "api", "State": "exited", "Health": ""},
            {"Service": "db", "State": "running", "Health": "healthy"},
        ]
    )
    with pytest.raises(AcceptanceError, match="api"):
        assert_compose_services_healthy(exited, {"api", "db"})

    with pytest.raises(AcceptanceError, match="missing"):
        assert_compose_services_healthy(healthy, {"api", "db", "worker"})


def test_compose_compatibility_exercises_every_declared_parser_fixture(
    tmp_path: Path,
) -> None:
    bundle = tmp_path / "vonk-forge"
    bundle.mkdir()
    (bundle / "docker-compose.yaml").write_text("services: {}\n")
    (bundle / ".env").write_text("COMPOSE_PROJECT_NAME=vonk-forge\n")
    log = tmp_path / "fixtures.log"
    fixtures = []
    for name in ("ugreen-compose-5.1.3", "lower-compose-2.24.6"):
        fixture = tmp_path / name
        fixture.write_text("#!/bin/sh\nprintf '%s\\n' \"$0:$*\" >> \"$FIXTURE_LOG\"\n")
        fixture.chmod(0o755)
        fixtures.append((name, fixture))

    assert_compose_compatibility(
        bundle,
        fixtures=fixtures,
        environment={"FIXTURE_LOG": str(log), "PATH": "/usr/bin:/bin"},
    )

    assert log.read_text().splitlines() == [
        f"{fixtures[0][1]}:-f docker-compose.yaml config --quiet",
        f"{fixtures[1][1]}:-f docker-compose.yaml config --quiet",
    ]


def test_compose_compatibility_rejects_all_parser_diagnostics(
    tmp_path: Path,
) -> None:
    bundle = tmp_path / "vonk-forge"
    bundle.mkdir()
    (bundle / "docker-compose.yaml").write_text("services: {}\n")
    (bundle / ".env").write_text("COMPOSE_PROJECT_NAME=vonk-forge\n")
    fixture = tmp_path / "compose-v5"
    fixture.write_text(
        "#!/bin/sh\n"
        "printf '%s\\n' 'the attribute `version` is obsolete, it will be ignored, please remove it to avoid potential confusion' >&2\n"
    )
    fixture.chmod(0o755)

    with pytest.raises(AcceptanceError, match="emitted output"):
        assert_compose_compatibility(
            bundle,
            fixtures=[("ugreen-compose-5.1.3", fixture)],
            environment={"PATH": "/usr/bin:/bin"},
        )


def test_write_all_retries_deterministic_partial_writes() -> None:
    received = bytearray()
    chunks = iter((2, 1, 3))

    def partial_write(data: bytes) -> int:
        count = next(chunks)
        received.extend(data[:count])
        return count

    write_all(partial_write, b"abcdef")

    assert received == b"abcdef"


def test_https_tunnel_performs_hostname_verified_tls_over_a_command(
    tmp_path: Path,
) -> None:
    key = tmp_path / "key.pem"
    certificate = tmp_path / "certificate.pem"
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            key,
            "-out",
            certificate,
            "-subj",
            "/CN=localhost",
            "-addext",
            "subjectAltName=DNS:localhost",
            "-days",
            "1",
        ],
        check=True,
        capture_output=True,
    )
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]

    def serve() -> None:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(certificate, key)
        connection, _ = listener.accept()
        with context.wrap_socket(connection, server_side=True) as tls:
            request = tls.recv(4096)
            assert request.startswith(b"GET /ready HTTP/1.1\r\nHost: localhost\r\n")
            tls.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok")
        listener.close()

    server = threading.Thread(target=serve)
    server.start()
    tunnel = tmp_path / "tunnel.py"
    tunnel.write_text(
        "import os,select,socket,sys\n"
        "peer=socket.create_connection(('127.0.0.1',int(sys.argv[1])))\n"
        "while True:\n"
        "    readable,_,_=select.select([0,peer],[],[])\n"
        "    if 0 in readable:\n"
        "        data=os.read(0,65536)\n"
        "        if not data: break\n"
        "        peer.sendall(data)\n"
        "    if peer in readable:\n"
        "        data=peer.recv(65536)\n"
        "        if not data: break\n"
        "        os.write(1,data)\n"
    )

    response = https_over_command(
        [sys.executable, tunnel, str(port)],
        server_hostname="localhost",
        path="/ready",
        cwd=tmp_path,
        environment={"PATH": "/usr/bin:/bin"},
        timeout=5,
        ca_file=certificate,
    )
    server.join(timeout=5)

    assert not server.is_alive()
    assert response.endswith(b"\r\n\r\nok")
