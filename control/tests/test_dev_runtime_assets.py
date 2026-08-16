from __future__ import annotations

import hashlib
import importlib
import importlib.resources
import importlib.util
import json
import os
import selectors
import shutil
import socket
import stat
import subprocess
import sys
import threading
import time
import types
import urllib.error
import urllib.request
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import ClassVar

import pytest

RESOURCE_PACKAGE = "vonk_control.resources.dev"
EXPECTED_RESOURCES = {
    "Caddyfile": 0o444,
    "caddy-entrypoint.sh": 0o555,
    "litellm-bootstrap.json": 0o444,
    "litellm-entrypoint.sh": 0o555,
    "litellm-supervisor.py": 0o555,
    "tailscale-configure.sh": 0o555,
}
MAXIMUM_RESOURCE_BYTES = 128 * 1024


def _runtime_assets():
    return importlib.import_module("vonk_control.dev_runtime_assets")


def _litellm_supervisor():
    path = importlib.resources.files(RESOURCE_PACKAGE).joinpath("litellm-supervisor.py")
    spec = importlib.util.spec_from_file_location("test_litellm_supervisor", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _litellm_document(*, model_name: str = "chat") -> dict[str, object]:
    models: list[dict[str, object]] = []
    if model_name:
        models.append(
            {
                "litellm_params": {
                    "api_base": "http://10.0.0.2:8000/v1",
                    "api_key": "os.environ/LITELLM_UPSTREAM_KEY",
                    "model": f"openai/{model_name}",
                    "rpm": 10,
                    "tpm": 1000,
                },
                "model_name": model_name,
            }
        )
    return {
        "general_settings": {
            "database_url": "os.environ/LITELLM_DATABASE_URL",
            "disable_admin_ui": False,
            "master_key": "os.environ/LITELLM_MASTER_KEY",
            "store_model_in_db": False,
        },
        "litellm_settings": {
            "drop_params": True,
            "failure_callback": [],
            "set_verbose": False,
            "success_callback": [],
        },
        "model_list": models,
        "router_settings": {
            "enable_pre_call_checks": True,
            "routing_strategy": "simple-shuffle",
        },
    }


def _canonical_json(document: dict[str, object]) -> bytes:
    return (json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _active_litellm_bundle(
    supervisor: object,
    tmp_path: Path,
    config: bytes,
    *,
    now: datetime,
    expires_at: datetime | None = None,
    generation: int = 1,
) -> Path:
    routes = b'{"routes":{"chat":{}},"state":"published"}\n'
    manifest = {
        "evidence_set_digest": "b" * 64,
        "expires_at": (expires_at or now + timedelta(seconds=120)).isoformat(),
        "generation": generation,
        "issued_at": (now - timedelta(seconds=1)).isoformat(),
        "litellm_sha256": hashlib.sha256(config).hexdigest(),
        "plan_digest": "a" * 64,
        "reconciliation_id": "bb7aac18-edbf-4cc1-bafd-15e282557c53",
        "routes_sha256": hashlib.sha256(routes).hexdigest(),
        "schema_version": 1,
        "state": "published",
    }
    manifest_content = _canonical_json(manifest)
    manifest_sha = hashlib.sha256(manifest_content).hexdigest()
    directory_name = f"{generation:08d}-{manifest_sha}"
    root = tmp_path / "routes"
    generation = root / "generations" / directory_name
    generation.mkdir(parents=True)
    (generation / "manifest.json").write_bytes(manifest_content)
    (generation / "routes.json").write_bytes(routes)
    selected = generation / "litellm.json"
    selected.write_bytes(config)
    activation = {
        **manifest,
        "directory": directory_name,
        "manifest_sha256": manifest_sha,
    }
    (root / "activation.json").write_bytes(_canonical_json(activation))
    supervisor.ROOT = root
    supervisor.ACTIVATION = root / "activation.json"
    supervisor.GENERATIONS = root / "generations"
    return selected


def test_development_route_lease_authority_matches_production_boundaries(
    tmp_path: Path,
) -> None:
    supervisor = _litellm_supervisor()
    now = datetime(2026, 8, 16, 12, 0, tzinfo=UTC)
    _active_litellm_bundle(
        supervisor,
        tmp_path,
        _canonical_json(_litellm_document()),
        now=now,
    )
    request = supervisor._active_request(now=now)
    assert request is not None
    expires_at = datetime.fromisoformat(str(request.marker["expires_at"]))
    authority = supervisor._RouteLeaseAuthority()

    authority.deny()
    assert authority.authorized(now) is False
    authority.allow_bootstrap()
    assert authority.authorized(now) is True
    authority.activate(request)
    request.marker["expires_at"] = now.isoformat()
    assert authority.authorized(expires_at - timedelta(microseconds=1)) is True
    assert authority.authorized(expires_at) is False
    assert authority.authorized(now.replace(tzinfo=None)) is False
    authority.activate(types.SimpleNamespace(marker={}))
    assert authority.authorized(now) is False


def _development_route_lease_status(
    server: object,
    *,
    path: str = "/vonk/route-lease",
    method: str = "GET",
) -> tuple[int, bytes, object]:
    host, port = server.server_address
    request = urllib.request.Request(f"http://{host}:{port}{path}", method=method)
    try:
        response = urllib.request.urlopen(request, timeout=1)
    except urllib.error.HTTPError as error:
        return error.code, error.read(), error.headers
    with response:
        return response.status, response.read(), response.headers


def _raw_development_route_lease_response(server: object, request: bytes) -> bytes:
    host, port = server.server_address
    with socket.create_connection((host, port), timeout=2) as connection:
        connection.sendall(request)
        connection.shutdown(socket.SHUT_WR)
        chunks: list[bytes] = []
        while chunk := connection.recv(65536):
            chunks.append(chunk)
    return b"".join(chunks)


@pytest.mark.parametrize(
    "raw_request",
    (
        pytest.param(
            b"GET /" + b"a" * 65536 + b" HTTP/1.1\r\nHost: x\r\n\r\n",
            id="overlong-request-line",
        ),
        pytest.param(
            b"GET / extra HTTP/1.1\r\nHost: x\r\n\r\n",
            id="malformed-request-line",
        ),
        pytest.param(
            b"GET / HTTP/nope\r\nHost: x\r\n\r\n",
            id="malformed-http-version",
        ),
        pytest.param(
            b"GET / HTTP/1.1\r\nX: " + b"a" * 65536 + b"\r\n\r\n",
            id="overlong-header-line",
        ),
    ),
)
def test_development_route_lease_http_maps_parser_errors_to_empty_404(
    raw_request: bytes,
) -> None:
    supervisor = _litellm_supervisor()
    server = supervisor._start_route_lease_server(
        supervisor._RouteLeaseAuthority(),
        host="127.0.0.1",
        port=0,
    )
    try:
        response = _raw_development_route_lease_response(server, raw_request)
    finally:
        server.shutdown()
        server.server_close()

    status, separator, remainder = response.partition(b"\r\n")
    headers, header_separator, body = remainder.partition(b"\r\n\r\n")
    assert status == b"HTTP/1.1 404 Not Found"
    assert separator == b"\r\n"
    assert header_separator == b"\r\n\r\n"
    assert b"Cache-Control: no-store\r\n" in headers + b"\r\n"
    assert b"Content-Length: 0\r\n" in headers + b"\r\n"
    assert b"Server:" not in headers
    assert body == b""


def test_development_route_lease_http_suppresses_interim_continue() -> None:
    supervisor = _litellm_supervisor()
    authority = supervisor._RouteLeaseAuthority()
    authority.allow_bootstrap()
    server = supervisor._start_route_lease_server(
        authority,
        host="127.0.0.1",
        port=0,
    )
    try:
        response = _raw_development_route_lease_response(
            server,
            b"GET /vonk/route-lease HTTP/1.1\r\n"
            b"Host: x\r\n"
            b"Connection: close\r\n"
            b"Expect: 100-continue\r\n"
            b"Content-Length: 1\r\n\r\n",
        )
    finally:
        server.shutdown()
        server.server_close()

    status, separator, remainder = response.partition(b"\r\n")
    headers, header_separator, body = remainder.partition(b"\r\n\r\n")
    assert response.count(b"HTTP/1.1 ") == 1
    assert status == b"HTTP/1.1 404 Not Found"
    assert separator == b"\r\n"
    assert header_separator == b"\r\n\r\n"
    assert b"Cache-Control: no-store\r\n" in headers + b"\r\n"
    assert b"Content-Length: 0\r\n" in headers + b"\r\n"
    assert b"Server:" not in headers
    assert body == b""


def test_development_route_lease_http_matches_production_boundaries(
    tmp_path: Path,
) -> None:
    supervisor = _litellm_supervisor()
    now = datetime.now(UTC)
    _active_litellm_bundle(
        supervisor,
        tmp_path,
        _canonical_json(_litellm_document()),
        now=now,
    )
    request = supervisor._active_request(now=now)
    assert request is not None
    authority = supervisor._RouteLeaseAuthority()
    server = supervisor._start_route_lease_server(
        authority,
        host="127.0.0.1",
        port=0,
    )
    try:
        status, body, headers = _development_route_lease_status(server)
        assert (status, body) == (503, b"")
        assert headers["Cache-Control"] == "no-store"
        assert "Server" not in headers

        authority.activate(request)
        assert _development_route_lease_status(server)[:2] == (204, b"")
        assert _development_route_lease_status(server, path="/health")[0] == 404
        assert _development_route_lease_status(server, method="POST")[0] == 404
        status, body, headers = _development_route_lease_status(server, method="BREW")
        assert (status, body) == (404, b"")
        assert "Server" not in headers

        authority.deny()
        assert _development_route_lease_status(server)[0] == 503
    finally:
        server.shutdown()
        server.server_close()


def test_development_resources_are_complete_bounded_regular_files() -> None:
    package = importlib.resources.files(RESOURCE_PACKAGE)

    for name in EXPECTED_RESOURCES:
        resource = package.joinpath(name)
        assert resource.is_file(), name
        assert not resource.is_symlink(), name
        content = resource.read_bytes()
        assert 0 < len(content) <= MAXIMUM_RESOURCE_BYTES, name


def test_stage_development_assets_atomically_replaces_complete_regular_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime_assets = _runtime_assets()
    destination = tmp_path / "runtime-config"
    destination.mkdir()
    for name in EXPECTED_RESOURCES:
        (destination / name).write_bytes(f"old-{name}\n".encode())
    real_replace = os.replace
    replacements: list[str] = []

    def inspect_replace(
        source: str | os.PathLike[str],
        target: str | os.PathLike[str],
        *,
        src_dir_fd: int | None = None,
        dst_dir_fd: int | None = None,
    ) -> None:
        assert src_dir_fd is not None
        assert dst_dir_fd == src_dir_fd
        target_name = os.fspath(target)
        assert (destination / target_name).read_bytes() == (
            f"old-{target_name}\n".encode()
        )
        source_metadata = os.stat(
            source,
            dir_fd=src_dir_fd,
            follow_symlinks=False,
        )
        assert stat.S_ISREG(source_metadata.st_mode)
        assert (
            os.stat(
                target,
                dir_fd=dst_dir_fd,
                follow_symlinks=False,
            ).st_nlink
            == 1
        )
        assert Path(f"/proc/self/fd/{src_dir_fd}").resolve() == destination
        source_content = os.open(
            source,
            os.O_RDONLY | os.O_NOFOLLOW,
            dir_fd=src_dir_fd,
        )
        try:
            with os.fdopen(source_content, "rb", closefd=False) as staged:
                assert (
                    staged.read()
                    == importlib.resources.files(RESOURCE_PACKAGE)
                    .joinpath(target_name)
                    .read_bytes()
                )
        finally:
            os.close(source_content)
        replacements.append(target_name)
        real_replace(
            source,
            target,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
        )

    monkeypatch.setattr(runtime_assets.os, "replace", inspect_replace)

    runtime_assets.stage_development_assets(RESOURCE_PACKAGE, destination)

    assert set(replacements) == set(EXPECTED_RESOURCES)
    assert {path.name for path in destination.iterdir()} == set(EXPECTED_RESOURCES)
    for name, mode in EXPECTED_RESOURCES.items():
        target = destination / name
        assert (
            target.read_bytes()
            == importlib.resources.files(RESOURCE_PACKAGE).joinpath(name).read_bytes()
        )
        assert stat.S_IMODE(target.stat().st_mode) == mode


def test_stage_development_assets_preserves_unchanged_live_mount_inodes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime_assets = _runtime_assets()
    destination = tmp_path / "runtime-config"
    runtime_assets.stage_development_assets(RESOURCE_PACKAGE, destination)
    identities = {
        name: (destination / name).stat().st_ino for name in EXPECTED_RESOURCES
    }

    def reject_replace(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("unchanged runtime assets must preserve their inode")

    monkeypatch.setattr(runtime_assets.os, "replace", reject_replace)
    runtime_assets.stage_development_assets(RESOURCE_PACKAGE, destination)

    assert {
        name: (destination / name).stat().st_ino for name in EXPECTED_RESOURCES
    } == identities


def test_stage_development_assets_assigns_exact_service_owners_when_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime_assets = _runtime_assets()
    ownership: dict[str, tuple[int, int]] = {}

    def record_owner(descriptor: int, uid: int, gid: int) -> None:
        name = Path(os.readlink(f"/proc/self/fd/{descriptor}")).name
        if name.startswith(".") and name.endswith(".new"):
            projected_name = name[1:].split(".", 1)[0]
            ownership[projected_name] = (uid, gid)

    monkeypatch.setattr(runtime_assets.os, "geteuid", lambda: 0)
    monkeypatch.setattr(runtime_assets.os, "fchown", record_owner)

    runtime_assets.stage_development_assets(
        RESOURCE_PACKAGE,
        tmp_path / "runtime-config",
    )

    assert ownership == {
        "Caddyfile": (10000, 10000),
        "caddy-entrypoint": (10000, 10000),
        "litellm-bootstrap": (10002, 10001),
        "litellm-entrypoint": (10002, 10001),
        "litellm-supervisor": (10002, 10001),
        "tailscale-configure": (0, 0),
    }


def test_stage_development_assets_rejects_a_symlink_target_without_touching_it(
    tmp_path: Path,
) -> None:
    runtime_assets = _runtime_assets()
    destination = tmp_path / "runtime-config"
    destination.mkdir()
    outside = tmp_path / "outside"
    outside.write_bytes(b"preserve\n")
    (destination / "Caddyfile").symlink_to(outside)

    with pytest.raises(runtime_assets.DevelopmentAssetError, match="unsafe"):
        runtime_assets.stage_development_assets(RESOURCE_PACKAGE, destination)

    assert (destination / "Caddyfile").is_symlink()
    assert outside.read_bytes() == b"preserve\n"


def test_stage_development_assets_rejects_a_symlink_package_resource(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime_assets = _runtime_assets()
    package_root = tmp_path / "test_assets"
    package_root.mkdir()
    (package_root / "__init__.py").write_bytes(b"")
    outside = tmp_path / "outside-resource"
    outside.write_bytes(b"do-not-stage\n")
    for name in EXPECTED_RESOURCES:
        target = package_root / name
        if name == "Caddyfile":
            target.symlink_to(outside)
        else:
            target.write_bytes(f"safe-{name}\n".encode())
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()

    with pytest.raises(runtime_assets.DevelopmentAssetError, match="unsafe"):
        runtime_assets.stage_development_assets(
            "test_assets",
            tmp_path / "runtime-config",
        )

    assert not (tmp_path / "runtime-config" / "Caddyfile").exists()
    assert outside.read_bytes() == b"do-not-stage\n"


def test_stage_development_assets_rejects_non_filesystem_resources_without_reading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime_assets = _runtime_assets()

    class NonFilesystemResource:
        def joinpath(self, _name: str) -> NonFilesystemResource:
            return self

        def read_bytes(self) -> bytes:
            raise AssertionError("non-filesystem resource must not be read")

    monkeypatch.setattr(
        runtime_assets.resources,
        "files",
        lambda _package: NonFilesystemResource(),
    )

    with pytest.raises(runtime_assets.DevelopmentAssetError, match="unsafe"):
        runtime_assets.stage_development_assets(
            "custom.provider",
            tmp_path / "runtime-config",
        )


def test_stage_development_assets_rejects_oversize_resource_before_reading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime_assets = _runtime_assets()
    package_root = tmp_path / "oversize_assets"
    package_root.mkdir()
    (package_root / "__init__.py").write_bytes(b"")
    for name in EXPECTED_RESOURCES:
        (package_root / name).write_bytes(
            b"x" * (MAXIMUM_RESOURCE_BYTES + 1)
            if name == "Caddyfile"
            else f"safe-{name}\n".encode()
        )
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()

    with pytest.raises(runtime_assets.DevelopmentAssetError, match="unsafe"):
        runtime_assets.stage_development_assets(
            "oversize_assets",
            tmp_path / "runtime-config",
        )

    assert not (tmp_path / "runtime-config").exists()


def test_caddy_entrypoint_stages_runtime_files_as_uid_10000(
    tmp_path: Path,
) -> None:
    if shutil.which("docker") is None:
        pytest.skip("Docker is required for the non-root Caddy entrypoint test")
    package = importlib.resources.files(RESOURCE_PACKAGE)
    entrypoint = Path(os.fspath(package.joinpath("caddy-entrypoint.sh")))
    secrets_root = tmp_path / "secrets"
    secrets_root.mkdir(mode=0o755)
    required = {
        "controller-server-certificate": b"certificate\n",
        "controller-server-key": b"private-key\n",
        "agent-ca-certificate": b"agent-ca\n",
        "agent-proxy-auth": b"A" * 43 + b"\n",
        "management-cidrs": b"192.0.2.0/24\n",
    }
    for name, content in required.items():
        target = secrets_root / name
        target.write_bytes(content)
        target.chmod(0o444)

    command = (
        "docker",
        "run",
        "--rm",
        "--user",
        "10000:10000",
        "--tmpfs",
        "/tmp:rw,mode=1777",
        "--tmpfs",
        "/run/vonk-caddy:rw,exec,mode=0700,uid=10000,gid=10000",
        "--mount",
        f"type=bind,src={entrypoint},dst=/entrypoint.sh,readonly",
        "--mount",
        f"type=bind,src={secrets_root},dst=/run/secrets,readonly",
        "--env",
        "VONK_CONTROL_HOSTNAME=vonk-forge.tailnet.test.ts.net",
        "--env",
        "VONK_AGENT_ENROLL_HOSTNAME=enroll.test",
        "--env",
        "VONK_AGENT_HOSTNAME=agent.test",
        "--env",
        "VONK_BACKEND_PORT=8443",
        "--entrypoint",
        "/bin/sh",
        "caddy:2.10.2@sha256:c3d7ee5d2b11f9dc54f947f68a734c84e9c9666c92c88a7f30b9cba5da182adb",
        "-c",
        (
            "exec /bin/sh /entrypoint.sh /bin/sh -c '"
            'test "$(stat -c %a /tmp/vonk-agent-proxy-auth.caddy)" = 400 '
            '&& test "$(wc -l < /tmp/vonk-agent-proxy-auth.caddy)" = 1 '
            '&& test "$(stat -c %a /run/vonk-caddy/caddy)" = 500 '
            "&& /run/vonk-caddy/caddy version >/dev/null'"
        ),
    )
    result = subprocess.run(
        command,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=False,
        timeout=30,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert "A" * 32 not in result.stdout + result.stderr

    absent_command = list(command)
    absent_index = absent_command.index("--env")
    while absent_command[absent_index + 1] != (
        "VONK_CONTROL_HOSTNAME=vonk-forge.tailnet.test.ts.net"
    ):
        absent_index = absent_command.index("--env", absent_index + 1)
    del absent_command[absent_index : absent_index + 2]
    absent = subprocess.run(
        absent_command,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=False,
        timeout=30,
        text=True,
    )
    assert absent.returncode != 0
    assert "VONK_CONTROL_HOSTNAME" in absent.stderr

    invalid_command = list(command)
    invalid_command[
        invalid_command.index("VONK_CONTROL_HOSTNAME=vonk-forge.tailnet.test.ts.net")
    ] = "VONK_CONTROL_HOSTNAME=control.test.example"
    invalid = subprocess.run(
        invalid_command,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=False,
        timeout=30,
        text=True,
    )
    assert invalid.returncode == 64
    assert "browser hostname must be vonk-forge.<tailnet-name>.ts.net" in invalid.stderr

    hostname_root = tmp_path / "tailnet-runtime"
    hostname_root.mkdir(mode=0o755)
    hostname_file = hostname_root / "control-hostname.ready"
    hostname_file.write_text(
        "11111111-1111-4111-8111-111111111111 vonk-forge.discovered-tailnet.ts.net\n",
        encoding="utf-8",
    )
    hostname_file.chmod(0o444)
    file_command = list(command)
    hostname_environment_index = file_command.index(
        "VONK_CONTROL_HOSTNAME=vonk-forge.tailnet.test.ts.net"
    )
    del file_command[hostname_environment_index - 1 : hostname_environment_index + 1]
    entrypoint_index = file_command.index("--entrypoint")
    file_command[entrypoint_index:entrypoint_index] = [
        "--mount",
        f"type=bind,src={hostname_root},dst=/run/vonk-tailnet,readonly",
        "--env",
        "VONK_CONTROL_HOSTNAME_FILE=/run/vonk-tailnet/control-hostname.ready",
        "--env",
        "VONK_CONTROL_HOSTNAME_GENERATION=11111111-1111-4111-8111-111111111111",
    ]
    discovered = subprocess.run(
        file_command,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=False,
        timeout=30,
        text=True,
    )
    assert discovered.returncode == 0, discovered.stderr

    hostname_file.unlink()
    pending = subprocess.run(
        file_command,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=False,
        timeout=30,
        text=True,
    )
    assert pending.returncode == 0, pending.stderr

    hostname_file.write_text(
        "11111111-1111-4111-8111-111111111111 control.test.example\n",
        encoding="utf-8",
    )
    hostname_file.chmod(0o444)
    rejected_file = subprocess.run(
        file_command,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=False,
        timeout=30,
        text=True,
    )
    assert rejected_file.returncode == 64
    assert "control.test.example" not in rejected_file.stdout + rejected_file.stderr


def _atomic_text(path: Path, content: str) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}")
    temporary.write_text(content, encoding="utf-8")
    temporary.chmod(0o444)
    os.replace(temporary, path)


def _next_handoff_mode(
    process: subprocess.Popen[str], selector: selectors.BaseSelector, *, timeout: float
) -> str:
    deadline = time.monotonic() + timeout
    observed: list[str] = []
    while time.monotonic() < deadline:
        events = selector.select(deadline - time.monotonic())
        if not events:
            break
        assert process.stdout is not None
        line = process.stdout.readline().strip()
        if not line and process.poll() is not None:
            break
        observed.append(line)
        if line == "agent-only" or line.startswith("browser:"):
            return line
    remainder = ""
    if process.poll() is not None and process.stdout is not None:
        remainder = process.stdout.read()
    raise AssertionError(
        f"Caddy handoff process did not report its mode; "
        f"returncode={process.poll()}, observed={observed!r}, output={remainder!r}"
    )


def test_caddy_entrypoint_requires_fresh_generation_and_reacts_to_real_file_events(
    tmp_path: Path,
) -> None:
    if shutil.which("docker") is None:
        pytest.skip("Docker is required for the Caddy hostname handoff test")
    package = importlib.resources.files(RESOURCE_PACKAGE)
    package_root = Path(os.fspath(package))
    secrets_root = tmp_path / "secrets"
    secrets_root.mkdir(mode=0o755)
    for name, content in {
        "controller-server-certificate": b"certificate\n",
        "controller-server-key": b"private-key\n",
        "agent-ca-certificate": b"agent-ca\n",
        "agent-proxy-auth": b"A" * 43 + b"\n",
        "management-cidrs": b"192.0.2.0/24\n",
    }.items():
        target = secrets_root / name
        target.write_bytes(content)
        target.chmod(0o444)
    tailnet_root = tmp_path / "tailnet"
    tailnet_root.mkdir(mode=0o755)
    authority = tailnet_root / "control-hostname.ready"
    _atomic_text(
        authority,
        "vonk-forge.yesterdays-tailnet.ts.net\n",
    )
    fake_caddy = tmp_path / "caddy"
    fake_caddy.write_text(
        "#!/bin/sh\n"
        "config=\n"
        'while [ "$#" -gt 0 ]; do\n'
        '  if [ "$1" = --config ]; then config=$2; shift 2; else shift; fi\n'
        "done\n"
        "if grep -Fq ':8080 {' \"$config\"; then\n"
        "  printf 'browser:%s\\n' \"$VONK_CONTROL_HOSTNAME\"\n"
        "else\n"
        "  printf 'agent-only\\n'\n"
        "fi\n"
        "trap 'exit 0' TERM INT\n"
        "while :; do sleep 1; done\n",
        encoding="utf-8",
    )
    fake_caddy.chmod(0o755)
    container_name = f"vonk-caddy-handoff-{uuid.uuid4().hex[:12]}"
    command = [
        "docker",
        "run",
        "--rm",
        "--name",
        container_name,
        "--user",
        "10000:10000",
        "--tmpfs",
        "/tmp:rw,mode=1777",
        "--tmpfs",
        "/run/vonk-caddy:rw,exec,mode=0700,uid=10000,gid=10000",
        "--mount",
        f"type=bind,src={package_root},dst=/run/vonk-runtime,readonly",
        "--mount",
        f"type=bind,src={secrets_root},dst=/run/secrets,readonly",
        "--mount",
        f"type=bind,src={tailnet_root},dst=/run/vonk-tailnet,readonly",
        "--mount",
        f"type=bind,src={fake_caddy},dst=/usr/bin/caddy,readonly",
        "--env",
        "VONK_CONTROL_HOSTNAME_FILE=/run/vonk-tailnet/control-hostname.ready",
        "--env",
        "VONK_AGENT_ENROLL_HOSTNAME=enroll.test",
        "--env",
        "VONK_AGENT_HOSTNAME=agent.test",
        "--env",
        "VONK_BACKEND_PORT=8443",
        "--env",
        "VONK_CADDY_HANDOFF_TEST_MODE=1",
        "--env",
        "VONK_CONTROL_HOSTNAME_POLL_INTERVAL=1",
        "--entrypoint",
        "/bin/sh",
        "caddy:2.10.2@sha256:c3d7ee5d2b11f9dc54f947f68a734c84e9c9666c92c88a7f30b9cba5da182adb",
        "/run/vonk-runtime/caddy-entrypoint.sh",
    ]
    process = subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    selector = selectors.DefaultSelector()
    assert process.stdout is not None
    selector.register(process.stdout, selectors.EVENT_READ)
    try:
        assert _next_handoff_mode(process, selector, timeout=10) == "agent-only"
        assert not selector.select(1.5), (
            "persisted authority must not enable browser mode"
        )

        _atomic_text(
            authority,
            "22222222-2222-4222-8222-222222222222 vonk-forge.current-tailnet.ts.net\n",
        )
        assert _next_handoff_mode(process, selector, timeout=10) == (
            "browser:vonk-forge.current-tailnet.ts.net"
        )

        _atomic_text(
            authority,
            "22222222-2222-4222-8222-222222222222 vonk-forge.replaced-tailnet.ts.net\n",
        )
        assert _next_handoff_mode(process, selector, timeout=10) == (
            "browser:vonk-forge.replaced-tailnet.ts.net"
        )

        authority.unlink()
        assert _next_handoff_mode(process, selector, timeout=10) == "agent-only"
    finally:
        selector.close()
        subprocess.run(
            ["docker", "rm", "-f", container_name],
            capture_output=True,
            check=False,
            text=True,
            timeout=10,
        )
        process.wait(timeout=10)


def test_caddy_health_requires_browser_authority_and_probes_the_browser_route(
    tmp_path: Path,
) -> None:
    entrypoint = Path(
        os.fspath(
            importlib.resources.files(RESOURCE_PACKAGE).joinpath("caddy-entrypoint.sh")
        )
    )
    authority = tmp_path / "control-hostname.ready"
    authority.write_text(
        "11111111-1111-4111-8111-111111111111 vonk-forge.test-tailnet.ts.net\n",
        encoding="ascii",
    )
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    calls = tmp_path / "wget.calls"
    wget = fake_bin / "wget"
    wget.write_text(
        '#!/bin/sh\nprintf \'%s\\n\' "$*" >>"$WGET_CALLS"\n',
        encoding="ascii",
    )
    wget.chmod(0o755)
    environment = os.environ | {
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "VONK_AGENT_ENROLL_HOSTNAME": "enroll.test",
        "VONK_AGENT_HOSTNAME": "agents.test",
        "VONK_BACKEND_PORT": "8443",
        "VONK_CONTROL_HOSTNAME_FILE": str(authority),
        "WGET_CALLS": str(calls),
    }

    healthy = subprocess.run(
        ["/bin/sh", entrypoint, "health"],
        env=environment,
        capture_output=True,
        check=False,
        text=True,
    )

    assert healthy.returncode == 0, healthy.stderr
    assert calls.read_text(encoding="ascii").splitlines() == [
        "-q --spider -T 3 http://127.0.0.1:2019/healthz",
        "-q --spider -T 3 --header=Host: vonk-forge.test-tailnet.ts.net "
        + "http://127.0.0.1:8080/healthz",
    ]

    authority.unlink()
    pending = subprocess.run(
        ["/bin/sh", entrypoint, "health"],
        env=environment,
        capture_output=True,
        check=False,
        text=True,
    )
    assert pending.returncode != 0


def test_litellm_supervisor_materializes_file_secrets_without_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    supervisor = _litellm_supervisor()
    secret_values = {
        "os.environ/LITELLM_DATABASE_URL": "postgresql://litellm:db@postgres/litellm",
        "os.environ/LITELLM_MASTER_KEY": "master-file-sentinel",
        "os.environ/LITELLM_UPSTREAM_KEY": "upstream-file-sentinel",
    }
    secret_paths: dict[str, Path] = {}
    for marker, value in secret_values.items():
        path = tmp_path / marker.removeprefix("os.environ/").lower()
        path.write_text(value + "\n", encoding="utf-8")
        secret_paths[marker] = path
        monkeypatch.setenv(marker.removeprefix("os.environ/"), "environment-leak")
    source = _canonical_json(_litellm_document())
    destination = tmp_path / "effective.json"
    monkeypatch.setattr(supervisor, "SECRET_FILES", secret_paths)

    effective = supervisor._materialize_config(source, destination=destination)

    assert effective == destination
    document = json.loads(destination.read_bytes())
    assert document["general_settings"] == {
        "database_url": secret_values["os.environ/LITELLM_DATABASE_URL"],
        "disable_admin_ui": True,
        "master_key": secret_values["os.environ/LITELLM_MASTER_KEY"],
        "store_model_in_db": False,
    }
    assert (
        document["model_list"][0]["litellm_params"]["api_key"]
        == (secret_values["os.environ/LITELLM_UPSTREAM_KEY"])
    )
    assert stat.S_IMODE(destination.stat().st_mode) == 0o400
    assert all(
        os.environ[name.removeprefix("os.environ/")] == "environment-leak"
        for name in secret_values
    )


def test_litellm_supervisor_uses_the_safer_v2_migration_resolver() -> None:
    supervisor = _litellm_supervisor()

    assert supervisor._litellm_command(Path("/tmp/effective.json")) == [
        "litellm",
        "--config",
        "/tmp/effective.json",
        "--host",
        "0.0.0.0",
        "--port",
        "4000",
        "--use_v2_migration_resolver",
    ]


def test_litellm_supervisor_allows_first_run_database_migrations() -> None:
    supervisor = _litellm_supervisor()

    assert supervisor.STARTUP_SECONDS == 120


def test_development_supervisor_recovers_from_a_transient_pre_health_child_exit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    supervisor = _litellm_supervisor()
    bootstrap = _canonical_json(_litellm_document(model_name=""))
    supervisor.ACK_ROOT = tmp_path / "supervisor"
    supervisor.ACK = supervisor.ACK_ROOT / "ack.json"
    supervisor.ACK_ROOT.mkdir()
    supervisor.ACK.write_text("stale\n")
    authority = supervisor._RouteLeaseAuthority()
    authority.allow_bootstrap()

    class Child:
        def __init__(self, *, pid: int, returncode: int) -> None:
            self.pid = pid
            self.returncode = returncode

        def poll(self) -> int:
            return self.returncode

    children = iter(
        (
            Child(pid=101, returncode=70),
            Child(pid=102, returncode=23),
        )
    )
    health = iter((False, True))
    spawns: list[Child] = []
    selections = 0
    materializations: list[bytes] = []
    retry_delays: list[float] = []
    authorization_at_cleanup: list[bool] = []
    original_clear_ack = supervisor._clear_ack

    def spawn(*_args, **_kwargs) -> Child:
        child = next(children)
        spawns.append(child)
        return child

    def clear_ack() -> None:
        authorization_at_cleanup.append(authority.authorized(datetime.now(UTC)))
        original_clear_ack()

    def active_request(**_kwargs):
        nonlocal selections
        selections += 1

    def materialize(source: bytes) -> Path:
        materializations.append(source)
        return tmp_path / "effective.json"

    def retry_sleep(seconds: float) -> None:
        assert authority.authorized(datetime.now(UTC)) is False
        assert not supervisor.ACK.exists()
        retry_delays.append(seconds)

    monkeypatch.setattr(supervisor, "_active_request", active_request)
    monkeypatch.setattr(supervisor, "_selected", lambda **_kwargs: bootstrap)
    monkeypatch.setattr(
        supervisor,
        "_materialize_config",
        materialize,
    )
    monkeypatch.setattr(
        supervisor,
        "_await_healthy",
        lambda _child: next(health),
    )
    monkeypatch.setattr(supervisor, "_clear_ack", clear_ack)
    monkeypatch.setattr(supervisor.subprocess, "Popen", spawn)
    monkeypatch.setattr(supervisor.signal, "signal", lambda *_args: None)
    monkeypatch.setattr(supervisor.time, "sleep", retry_sleep)

    assert supervisor._supervise(authority) == 23
    assert [child.pid for child in spawns] == [101, 102]
    assert selections == 2
    assert materializations == [bootstrap, bootstrap]
    assert retry_delays == [1]
    assert authorization_at_cleanup[0] is False
    assert authority.authorized(datetime.now(UTC)) is False
    assert not supervisor.ACK.exists()


def test_development_supervisor_bounds_pre_health_child_exit_retries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    supervisor = _litellm_supervisor()
    bootstrap = _canonical_json(_litellm_document(model_name=""))
    authority = supervisor._RouteLeaseAuthority()
    spawns = 0
    retry_delays: list[float] = []

    class Child:
        pid = 101
        returncode = 70

        @staticmethod
        def poll() -> int:
            return 70

    def spawn(*_args, **_kwargs) -> Child:
        nonlocal spawns
        spawns += 1
        return Child()

    monkeypatch.setattr(supervisor, "_active_request", lambda **_kwargs: None)
    monkeypatch.setattr(supervisor, "_selected", lambda **_kwargs: bootstrap)
    monkeypatch.setattr(
        supervisor,
        "_materialize_config",
        lambda _source: tmp_path / "effective.json",
    )
    monkeypatch.setattr(supervisor, "_await_healthy", lambda _child: False)
    monkeypatch.setattr(supervisor, "_clear_ack", lambda: None)
    monkeypatch.setattr(supervisor.subprocess, "Popen", spawn)
    monkeypatch.setattr(supervisor.signal, "signal", lambda *_args: None)
    monkeypatch.setattr(supervisor.time, "sleep", retry_delays.append)

    assert supervisor._supervise(authority) == 1
    assert spawns == 10
    assert retry_delays == [1] * 9
    assert authority.authorized(datetime.now(UTC)) is False


def test_development_supervisor_does_not_retry_a_live_child_after_health_deadline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    supervisor = _litellm_supervisor()
    bootstrap = _canonical_json(_litellm_document(model_name=""))
    authority = supervisor._RouteLeaseAuthority()
    spawns = 0

    class Child:
        pid = 101
        returncode = None
        terminated = False

        def poll(self):
            return self.returncode

        def terminate(self) -> None:
            self.terminated = True
            self.returncode = 0

        def wait(self, timeout=None):
            del timeout
            return self.returncode

    child = Child()

    def spawn(*_args, **_kwargs) -> Child:
        nonlocal spawns
        spawns += 1
        return child

    monkeypatch.setattr(supervisor, "_active_request", lambda **_kwargs: None)
    monkeypatch.setattr(supervisor, "_selected", lambda **_kwargs: bootstrap)
    monkeypatch.setattr(
        supervisor,
        "_materialize_config",
        lambda _source: tmp_path / "effective.json",
    )
    monkeypatch.setattr(supervisor, "_await_healthy", lambda _child: False)
    monkeypatch.setattr(supervisor, "_clear_ack", lambda: None)
    monkeypatch.setattr(supervisor.subprocess, "Popen", spawn)
    monkeypatch.setattr(supervisor.signal, "signal", lambda *_args: None)
    monkeypatch.setattr(
        supervisor.time,
        "sleep",
        lambda _seconds: pytest.fail("live unhealthy child was retried"),
    )

    assert supervisor._supervise(authority) == 1
    assert spawns == 1
    assert child.terminated is True
    assert authority.authorized(datetime.now(UTC)) is False


def test_development_supervisor_renews_same_config_route_lease_without_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    supervisor = _litellm_supervisor()
    issued_at = datetime.now(UTC)
    old_expires_at = issued_at + timedelta(seconds=120)
    new_expires_at = issued_at + timedelta(seconds=240)
    config = _canonical_json(_litellm_document())
    _active_litellm_bundle(
        supervisor,
        tmp_path,
        config,
        now=issued_at,
        expires_at=old_expires_at,
    )
    first_request = supervisor._active_request(now=issued_at)
    _active_litellm_bundle(
        supervisor,
        tmp_path,
        config,
        now=issued_at,
        expires_at=new_expires_at,
        generation=2,
    )
    second_request = supervisor._active_request(now=issued_at)
    assert first_request is not None and second_request is not None
    assert first_request.config_bytes == second_request.config_bytes
    supervisor.ACK_ROOT = tmp_path / "supervisor"
    supervisor.ACK = supervisor.ACK_ROOT / "ack.json"

    class Child:
        pid = 654
        returncode = None

        def poll(self):
            return self.returncode

        def terminate(self) -> None:
            self.returncode = 0

        def kill(self) -> None:
            self.returncode = -9

        def wait(self, timeout=None):
            del timeout
            return self.returncode

    class Server:
        def shutdown(self) -> None:
            return

        def server_close(self) -> None:
            return

    class Timer:
        instances: ClassVar[list[Timer]] = []

        def __init__(self, interval, function) -> None:
            self.interval = interval
            self.function = function
            self.cancelled = False
            self.daemon = False
            self.instances.append(self)

        def start(self) -> None:
            return

        def cancel(self) -> None:
            self.cancelled = True

    child = Child()
    requests = iter((first_request, second_request))
    authorities = []
    spawn_count = 0
    renewal_observations: dict[str, object] = {}
    renewal_order: list[str] = []
    original_write_ack = supervisor._write_ack
    original_guard = supervisor._ServingLeaseGuard

    def start_server(authority, **_kwargs):
        authorities.append(authority)
        original_activate = authority.activate

        def activate(request) -> None:
            original_activate(request)
            if request.marker["generation"] == 2:
                renewal_order.append("activate")

        authority.activate = activate
        return Server()

    def spawn(*_args, **_kwargs):
        nonlocal spawn_count
        spawn_count += 1
        assert spawn_count == 1, "same-config renewal restarted child"
        return child

    def write_ack(request, loaded_child, *, now):
        original_write_ack(request, loaded_child, now=now)
        if request.marker["generation"] == 2:
            renewal_order.append("ack")
            renewal_observations["authorized_after_old_expiry"] = authorities[
                0
            ].authorized(old_expires_at + timedelta(microseconds=1))
            renewal_observations["ack_generation"] = json.loads(
                supervisor.ACK.read_bytes()
            )["generation"]
            renewal_observations["timers"] = tuple(Timer.instances)
            child.returncode = 29

    class Guard(original_guard):
        def renew(self, request) -> None:
            if request is not None and request.marker["generation"] == 2:
                renewal_order.append("renew")
                assert authorities[0].authorized(
                    old_expires_at + timedelta(microseconds=1)
                )
            super().renew(request)

        def publish_ack(self, request, *, now) -> None:
            if request.marker["generation"] == 2:
                renewal_order.append("publish")
            super().publish_ack(request, now=now)

    monkeypatch.setattr(
        supervisor,
        "_active_request",
        lambda **_kwargs: next(requests),
    )
    monkeypatch.setattr(supervisor, "_await_healthy", lambda _child: True)
    monkeypatch.setattr(supervisor, "_healthy", lambda _child: True)
    monkeypatch.setattr(supervisor, "_materialize_config", lambda _source: tmp_path)
    monkeypatch.setattr(supervisor, "_start_route_lease_server", start_server)
    monkeypatch.setattr(supervisor, "_write_ack", write_ack)
    monkeypatch.setattr(supervisor, "_ServingLeaseGuard", Guard)
    monkeypatch.setattr(supervisor.subprocess, "Popen", spawn)
    monkeypatch.setattr(supervisor.signal, "signal", lambda *_args: None)
    monkeypatch.setattr(supervisor.threading, "Timer", Timer)
    monkeypatch.setattr(supervisor.time, "sleep", lambda _seconds: None)

    assert supervisor.main() == 29
    assert spawn_count == 1
    assert renewal_observations["authorized_after_old_expiry"] is True
    assert renewal_observations["ack_generation"] == 2
    assert renewal_order == ["activate", "renew", "publish", "ack"]
    timers = renewal_observations["timers"]
    assert isinstance(timers, tuple) and len(timers) == 2
    assert timers[0].cancelled is True
    assert timers[1].interval > timers[0].interval


def test_development_same_config_renewal_denies_and_aborts_when_rearm_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    supervisor = _litellm_supervisor()
    issued_at = datetime.now(UTC)
    old_expires_at = issued_at + timedelta(seconds=120)
    config = _canonical_json(_litellm_document())
    _active_litellm_bundle(
        supervisor,
        tmp_path,
        config,
        now=issued_at,
        expires_at=old_expires_at,
    )
    first_request = supervisor._active_request(now=issued_at)
    _active_litellm_bundle(
        supervisor,
        tmp_path,
        config,
        now=issued_at,
        expires_at=issued_at + timedelta(seconds=240),
        generation=2,
    )
    second_request = supervisor._active_request(now=issued_at)
    assert first_request is not None and second_request is not None
    supervisor.ACK_ROOT = tmp_path / "supervisor"
    supervisor.ACK = supervisor.ACK_ROOT / "ack.json"
    authority = supervisor._RouteLeaseAuthority()
    requests = iter((first_request, second_request))
    observed: dict[str, bool] = {}

    class Child:
        pid = 654

        @staticmethod
        def poll():
            return None

    class Guard:
        expired = False

        def __init__(self, _request, child, *, authority) -> None:
            self.authority = authority
            self.child = child

        def start(self) -> None:
            return

        def cancel(self) -> None:
            return

        def publish_ack(self, request, *, now) -> None:
            supervisor._write_ack(request, self.child, now=now)

        def renew(self, request) -> None:
            assert request.marker["generation"] == 2
            observed["new_snapshot_installed"] = self.authority.authorized(
                old_expires_at + timedelta(microseconds=1)
            )
            raise RuntimeError("simulated rearm failure")

    monkeypatch.setattr(
        supervisor,
        "_active_request",
        lambda **_kwargs: next(requests),
    )
    monkeypatch.setattr(supervisor, "_await_healthy", lambda _child: True)
    monkeypatch.setattr(supervisor, "_healthy", lambda _child: True)
    monkeypatch.setattr(supervisor, "_materialize_config", lambda _source: tmp_path)
    monkeypatch.setattr(supervisor, "_ServingLeaseGuard", Guard)
    monkeypatch.setattr(
        supervisor.subprocess,
        "Popen",
        lambda *_args, **_kwargs: Child(),
    )
    monkeypatch.setattr(supervisor.signal, "signal", lambda *_args: None)
    monkeypatch.setattr(supervisor.time, "sleep", lambda _seconds: None)

    with pytest.raises(RuntimeError, match="simulated rearm failure"):
        supervisor._supervise(authority)

    assert observed["new_snapshot_installed"] is True
    assert authority.authorized(issued_at) is False
    assert not supervisor.ACK.exists()


def test_development_supervisor_denies_malformed_activation_before_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    supervisor = _litellm_supervisor()
    now = datetime.now(UTC)
    bootstrap = _canonical_json(_litellm_document(model_name=""))
    _active_litellm_bundle(
        supervisor,
        tmp_path,
        _canonical_json(_litellm_document()),
        now=now,
    )
    supervisor.ACTIVATION.write_bytes(b"not-json\n")

    class Child:
        pid = 789
        returncode = None

        def poll(self):
            return self.returncode

    class Server:
        def shutdown(self) -> None:
            return

        def server_close(self) -> None:
            return

    child = Child()
    authorities = []
    authorization_at_cleanup: list[bool] = []

    def start_server(authority, **_kwargs):
        authorities.append(authority)
        return Server()

    def clear_ack() -> None:
        authorization_at_cleanup.append(authorities[0].authorized(now))
        child.returncode = 11

    monkeypatch.setattr(supervisor, "_start_route_lease_server", start_server)
    monkeypatch.setattr(supervisor, "_selected", lambda **_kwargs: bootstrap)
    monkeypatch.setattr(supervisor, "_materialize_config", lambda _source: tmp_path)
    monkeypatch.setattr(supervisor, "_await_healthy", lambda _child: True)
    monkeypatch.setattr(supervisor, "_clear_ack", clear_ack)
    monkeypatch.setattr(
        supervisor.subprocess,
        "Popen",
        lambda *_args, **_kwargs: child,
    )
    monkeypatch.setattr(supervisor.signal, "signal", lambda *_args: None)

    assert supervisor.main() == 11
    assert authorization_at_cleanup[0] is False


def test_development_expiry_denies_authority_before_ack_or_process_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    supervisor = _litellm_supervisor()
    expires_at = datetime(2026, 8, 16, 12, 0, tzinfo=UTC)
    request = types.SimpleNamespace(marker={"expires_at": expires_at.isoformat()})
    authority = supervisor._RouteLeaseAuthority()
    authority.allow_bootstrap()
    events: list[tuple[str, bool]] = []

    class Child:
        @staticmethod
        def poll():
            return None

        @staticmethod
        def kill() -> None:
            events.append(("kill", authority.authorized(expires_at)))

    monkeypatch.setattr(
        supervisor,
        "_clear_ack",
        lambda: events.append(("clear", authority.authorized(expires_at))),
    )
    guard = supervisor._ServingLeaseGuard(
        request,
        Child(),
        authority=authority,
        clock=lambda: expires_at,
    )

    guard.start()

    assert events == [("clear", False), ("kill", False)]


def test_development_expiry_claim_serializes_ack_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    supervisor = _litellm_supervisor()
    issued_at = datetime.now(UTC)
    _active_litellm_bundle(
        supervisor,
        tmp_path,
        _canonical_json(_litellm_document()),
        now=issued_at,
    )
    request = supervisor._active_request(now=issued_at)
    assert request is not None
    supervisor.ACK_ROOT = tmp_path / "supervisor"
    supervisor.ACK = supervisor.ACK_ROOT / "ack.json"
    authority = supervisor._RouteLeaseAuthority()
    authority.activate(request)
    write_entered = threading.Event()
    release_write = threading.Event()
    write_published = threading.Event()
    release_return = threading.Event()
    clear_entered = threading.Event()
    errors: list[BaseException] = []
    original_atomic_write = supervisor._atomic_write
    original_clear_ack = supervisor._clear_ack

    class Child:
        pid = 321
        returncode = None

        def poll(self):
            return self.returncode

        def kill(self) -> None:
            self.returncode = -9

    class Timer:
        instances: ClassVar[list[Timer]] = []

        def __init__(self, interval, function) -> None:
            self.interval = interval
            self.function = function
            self.daemon = False
            self.instances.append(self)

        def start(self) -> None:
            return

        def cancel(self) -> None:
            return

    child = Child()

    def blocked_atomic_write(target, content) -> None:
        write_entered.set()
        assert release_write.wait(2), "test did not release acknowledgement write"
        original_atomic_write(target, content)
        write_published.set()
        assert release_return.wait(2), "test did not release acknowledgement return"

    def observed_clear_ack() -> None:
        clear_entered.set()
        original_clear_ack()

    def publish() -> None:
        try:
            publisher = getattr(guard, "publish_ack", None)
            if publisher is None:
                supervisor._write_ack(request, child, now=issued_at)
            else:
                publisher(request, now=issued_at)
        except (AssertionError, OSError, RuntimeError) as error:
            errors.append(error)

    def expire() -> None:
        try:
            Timer.instances[0].function()
        except (AssertionError, OSError, RuntimeError) as error:
            errors.append(error)

    monkeypatch.setattr(supervisor, "_atomic_write", blocked_atomic_write)
    monkeypatch.setattr(supervisor, "_clear_ack", observed_clear_ack)
    monkeypatch.setattr(supervisor.threading, "Timer", Timer)
    guard = supervisor._ServingLeaseGuard(
        request,
        child,
        authority=authority,
        clock=lambda: issued_at,
    )
    guard.start()
    writer = threading.Thread(target=publish)
    expiry = threading.Thread(target=expire)
    writer.start()
    assert write_entered.wait(2), "acknowledgement write did not reach interleaving"
    expiry.start()
    expiry_cleared_while_write_pending = clear_entered.wait(0.1)
    release_write.set()
    assert write_published.wait(2), "acknowledgement was not published"
    release_return.set()
    writer.join(timeout=2)
    expiry.join(timeout=2)

    assert not writer.is_alive() and not expiry.is_alive()
    assert errors == []
    assert expiry_cleared_while_write_pending is False
    assert clear_entered.is_set()
    assert not supervisor.ACK.exists()


def test_development_expired_serving_guard_cannot_be_renewed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    supervisor = _litellm_supervisor()
    expires_at = datetime(2026, 8, 16, 12, 0, tzinfo=UTC)
    expired = types.SimpleNamespace(marker={"expires_at": expires_at.isoformat()})
    renewed = types.SimpleNamespace(
        marker={"expires_at": (expires_at + timedelta(seconds=1)).isoformat()}
    )

    class Child:
        @staticmethod
        def poll():
            return None

        @staticmethod
        def kill() -> None:
            return

    monkeypatch.setattr(supervisor, "_clear_ack", lambda: None)
    guard = supervisor._ServingLeaseGuard(
        expired,
        Child(),
        clock=lambda: expires_at,
    )
    guard.start()
    assert guard.expired is True

    try:
        with pytest.raises(RuntimeError, match="already expired"):
            guard.renew(renewed)
    finally:
        guard.cancel()


@pytest.mark.parametrize(
    ("marker", "fault"),
    (
        ("os.environ/LITELLM_MASTER_KEY", "wrong-position"),
        ("os.environ/LITELLM_MASTER_KEY", "duplicate"),
        ("os.environ/LITELLM_DATABASE_URL", "wrong-position"),
        ("os.environ/LITELLM_DATABASE_URL", "duplicate"),
        ("os.environ/LITELLM_UPSTREAM_KEY", "wrong-position"),
        ("os.environ/LITELLM_UPSTREAM_KEY", "duplicate"),
    ),
)
def test_litellm_supervisor_rejects_privileged_markers_outside_exact_schema_positions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, marker: str, fault: str
) -> None:
    supervisor = _litellm_supervisor()
    document = _litellm_document()
    router = document["router_settings"]
    assert isinstance(router, dict)
    if fault == "wrong-position":
        general = document["general_settings"]
        models = document["model_list"]
        assert isinstance(general, dict)
        assert isinstance(models, list) and isinstance(models[0], dict)
        if marker == "os.environ/LITELLM_MASTER_KEY":
            general["master_key"] = "literal-master-key"
            router["routing_strategy"] = marker
        elif marker == "os.environ/LITELLM_DATABASE_URL":
            general["database_url"] = "postgresql://literal/database"
            router["routing_strategy"] = marker
        else:
            parameters = models[0]["litellm_params"]
            assert isinstance(parameters, dict)
            parameters["api_key"] = "literal-upstream-key"
            models[0]["model_name"] = marker
    else:
        router["duplicate_privileged_marker"] = marker
    monkeypatch.setattr(
        supervisor,
        "_read_secret",
        lambda _path: (_ for _ in ()).throw(AssertionError("must validate first")),
    )

    with pytest.raises(RuntimeError, match="selected config is invalid"):
        supervisor._materialize_config(
            _canonical_json(document),
            destination=tmp_path / "effective.json",
        )


def test_litellm_supervisor_materializes_the_exact_verified_bytes_after_path_swap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    supervisor = _litellm_supervisor()
    now = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)
    verified = _canonical_json(_litellm_document(model_name="verified"))
    selected = _active_litellm_bundle(
        supervisor,
        tmp_path,
        verified,
        now=now,
    )
    secret_values = {
        "os.environ/LITELLM_DATABASE_URL": "postgresql://litellm:db@postgres/litellm",
        "os.environ/LITELLM_MASTER_KEY": "master-file-sentinel",
        "os.environ/LITELLM_UPSTREAM_KEY": "upstream-file-sentinel",
    }
    secret_paths: dict[str, Path] = {}
    for marker, value in secret_values.items():
        path = tmp_path / hashlib.sha256(marker.encode()).hexdigest()
        path.write_text(value + "\n", encoding="utf-8")
        secret_paths[marker] = path
    monkeypatch.setattr(supervisor, "SECRET_FILES", secret_paths)

    request = supervisor._active_request(now=now)
    assert request is not None
    selected.write_bytes(_canonical_json(_litellm_document(model_name="swapped")))
    destination = tmp_path / "effective.json"
    supervisor._materialize_config(request.config_bytes, destination=destination)

    effective = json.loads(destination.read_bytes())
    assert effective["model_list"][0]["model_name"] == "verified"
    assert request.config_sha256 == hashlib.sha256(verified).hexdigest()
