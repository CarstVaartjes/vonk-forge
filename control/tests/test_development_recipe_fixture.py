from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from http.client import HTTPConnection
from pathlib import Path

import pytest
from vonk_control.recipe_contract import recipe_content_sha256, validate_recipe
from vonk_control.source_bundles import generate_source_bundle
from vonk_control.source_policy import enforce_build_source_policy

FIXTURE_ROOT = Path(__file__).parent / "fixtures/recipes/dev-http-smoke"
CONTEXT_ROOT = FIXTURE_ROOT / "context"
PROJECT_ROOT = Path(__file__).resolve().parents[2]
EXPECTED_SOURCE_SHA256 = (
    "4220553fbdd61b6bea80e593c9085686862503a9df904e6fbb16d695d26776f5"
)
EXPECTED_RECIPE_SHA256 = (
    "f0b615191dd59e5baf6d988569e41173a48521394f4511b9f3f88ec44abfb0c9"
)
EXPECTED_ARTIFACT_SOURCE = (
    "https://raw.githubusercontent.com/CarstVaartjes/vonk-forge/"
    "8c03b33ebcef859fa9cecd715ec000b9dbc00f4a/"
    "tests/fixtures/node-health/healthy/commands/hostname.txt"
)
EXPECTED_ARTIFACT_SHA256 = (
    "8f9e3902c909d7698aac45b2d9195c4baea090ee35b11705f05ea10c856bd230"
)
EXPECTED_ARCHIVE_BYTES = 10240
EXPECTED_TOTAL_BYTES = 2504
MEASURED_IMAGE_BYTES = 150_054_446
MEASURED_DOCKER_ARCHIVE_BYTES = 154_805_248
BUILD_OUTPUT_LIMIT_BYTES = 256 * 1024 * 1024
BUILD_TRANSIENT_LIMIT_BYTES = 512 * 1024 * 1024
ROOTLESS_PODMAN_PHYSICAL_GATE = (
    "physical gate pending: requires rootless Podman on linux/arm64 with public "
    "access to the pinned python:3.12.11-slim-bookworm image"
)
EXPECTED_FILE_SHA256 = {
    "Dockerfile": "701a82b82b5056c17eb3dcf1e6c4d281e359ad771bc29a84c94715327e8f8257",
    "server.py": "c0e5517d35e8e19328e925210120d2374d751df6958a10ab67aaa9adf55d8c50",
}
EXPECTED_BASE_IMAGE = (
    "python:3.12.11-slim-bookworm@sha256:"
    "519591d6871b7bc437060736b9f7456b8731f1499a57e22e6c285135ae657bf7"
)
FORBIDDEN_DOCKERFILE_SNIPPETS = (
    "apt-get",
    "apk add",
    "dnf install",
    "yum install",
    "microdnf",
    "pip install",
    "curl ",
    "wget ",
)


def _read_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def fixture_recipe() -> dict[str, object]:
    return _read_json(FIXTURE_ROOT / "recipe.json")


def fixture_expected() -> dict[str, object]:
    return _read_json(FIXTURE_ROOT / "expected.json")


def test_fixture_artifact_is_an_immutable_public_smoke_payload() -> None:
    recipe = fixture_recipe()
    artifact = recipe["artifacts"][0]
    expected_bytes = (
        PROJECT_ROOT / "tests/fixtures/node-health/healthy/commands/hostname.txt"
    ).read_bytes()

    assert artifact == {
        "id": "fixture-contract",
        "kind": "http.file",
        "repository": EXPECTED_ARTIFACT_SOURCE,
        "revision": f"sha256:{EXPECTED_ARTIFACT_SHA256}",
        "download_bytes": len(expected_bytes),
        "installed_bytes": len(expected_bytes),
        "mount": {"target": "/models", "read_only": True},
        "roles": ["entrypoint"],
    }
    assert hashlib.sha256(expected_bytes).hexdigest() == EXPECTED_ARTIFACT_SHA256


def fixture_bundle():
    files = {
        path.relative_to(CONTEXT_ROOT).as_posix(): path.read_bytes()
        for path in sorted(CONTEXT_ROOT.rglob("*"))
        if path.is_file()
    }
    return generate_source_bundle(files)


def _request_json(
    host: str,
    port: int,
    method: str,
    path: str,
    payload: dict[str, object] | None = None,
) -> tuple[int, object]:
    connection = HTTPConnection(host, port, timeout=2)
    try:
        body = None
        headers: dict[str, str] = {}
        if payload is not None:
            body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
            headers["Content-Type"] = "application/json"
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        data = response.read()
    finally:
        connection.close()
    return response.status, json.loads(data) if data else None


def _unused_port(host: str = "127.0.0.1") -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind((host, 0))
        listener.listen()
        return int(listener.getsockname()[1])


@contextmanager
def running_fixture_server() -> Iterator[int]:
    host = "127.0.0.2"
    port = _unused_port(host)
    process = subprocess.Popen(
        [sys.executable, str(CONTEXT_ROOT / "server.py")],
        env={
            **os.environ,
            "VONK_LISTEN_HOST": host,
            "VONK_LISTEN_PORT": str(port),
        },
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.time() + 10
        last_error: BaseException | None = None
        while time.time() < deadline:
            if process.poll() is not None:
                stdout, stderr = process.communicate()
                raise AssertionError(
                    "fixture server exited before becoming healthy\n"
                    f"stdout:\n{stdout}\n"
                    f"stderr:\n{stderr}"
                )
            try:
                status, payload = _request_json(host, port, "GET", "/health")
            except OSError as error:
                last_error = error
                time.sleep(0.1)
                continue
            if status == 200 and isinstance(payload, dict):
                break
            last_error = AssertionError(f"unexpected health response: {status} {payload!r}")
            time.sleep(0.1)
        else:
            stdout, stderr = process.communicate(timeout=1)
            raise AssertionError(
                "fixture server did not become healthy within 10 seconds\n"
                f"stdout:\n{stdout}\n"
                f"stderr:\n{stderr}"
            ) from last_error
        yield port
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


def test_dev_http_smoke_fixture_is_schema_valid_and_hash_locked() -> None:
    recipe = fixture_recipe()
    bundle = fixture_bundle()

    validate_recipe(recipe)
    report = enforce_build_source_policy(recipe, bundle)

    assert report.passed is True
    assert bundle.sha256 == EXPECTED_SOURCE_SHA256
    assert len(bundle.archive) == EXPECTED_ARCHIVE_BYTES
    assert bundle.manifest.total_bytes == EXPECTED_TOTAL_BYTES
    assert recipe["build"]["context"] == {
        "sha256": EXPECTED_SOURCE_SHA256,
        "expected_bytes": EXPECTED_ARCHIVE_BYTES,
        "media_type": "application/vnd.vonk-forge.source-bundle.v1+tar",
    }
    assert recipe_content_sha256(recipe) == EXPECTED_RECIPE_SHA256


def test_dev_http_smoke_bundle_policy_is_exact_and_bounded() -> None:
    bundle = fixture_bundle()
    dockerfile = (CONTEXT_ROOT / "Dockerfile").read_text(encoding="utf-8")
    final_instruction = [line for line in dockerfile.splitlines() if line][-1]
    lowered = dockerfile.lower()

    assert [item.path for item in bundle.manifest.files] == ["Dockerfile", "server.py"]
    assert {item.path: item.sha256 for item in bundle.manifest.files} == EXPECTED_FILE_SHA256
    assert dockerfile.splitlines()[0] == f"FROM {EXPECTED_BASE_IMAGE}"
    assert final_instruction == "USER 10001:10001"
    assert "RUN " not in dockerfile
    assert "ADD " not in dockerfile
    assert all(snippet not in lowered for snippet in FORBIDDEN_DOCKERFILE_SNIPPETS)


def test_dev_http_smoke_limits_cover_measured_python_slim_build_and_export() -> None:
    recipe = fixture_recipe()
    build_resources = recipe["build"]["resources"]
    profile = recipe["deployment_profiles"][0]
    disk = profile["roles"][0]["resources"]["disk"]

    assert BUILD_OUTPUT_LIMIT_BYTES >= MEASURED_DOCKER_ARCHIVE_BYTES + 64 * 1024 * 1024
    assert BUILD_TRANSIENT_LIMIT_BYTES >= (
        MEASURED_IMAGE_BYTES + MEASURED_DOCKER_ARCHIVE_BYTES + 128 * 1024 * 1024
    )
    assert build_resources["temporary_bytes"] == BUILD_TRANSIENT_LIMIT_BYTES
    assert build_resources["timeout_seconds"] == 300
    assert disk["image_bytes"] == BUILD_OUTPUT_LIMIT_BYTES
    assert disk["staging_bytes"] == BUILD_TRANSIENT_LIMIT_BYTES
    assert disk["safety_margin_bytes"] == BUILD_TRANSIENT_LIMIT_BYTES
    assert profile["measurement"] == "measured"


def test_dev_http_smoke_server_matches_expected_health_and_inference() -> None:
    recipe = fixture_recipe()
    expected = fixture_expected()

    with running_fixture_server() as port:
        status, health = _request_json(
            "127.0.0.2",
            port,
            "GET",
            str(recipe["runtime"]["endpoint"]["health_path"]),
        )
        assert status == 200
        assert health == expected["health"]

        status, response = _request_json(
            "127.0.0.2",
            port,
            "POST",
            "/v1/chat/completions",
            expected["request"],
        )
        assert status == 200
        assert response == expected["response"]


def test_dev_http_smoke_build_import_and_host_published_port_with_rootless_podman(
    tmp_path: Path,
) -> None:
    podman = shutil.which("podman")
    if podman is None or platform.machine() not in {"aarch64", "arm64"}:
        pytest.skip(ROOTLESS_PODMAN_PHYSICAL_GATE)
    information = subprocess.run(
        [podman, "info", "--format", "{{.Host.Security.Rootless}}"],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if information.returncode != 0 or information.stdout.strip() != "true":
        pytest.skip(ROOTLESS_PODMAN_PHYSICAL_GATE)

    recipe = fixture_recipe()
    disk = recipe["deployment_profiles"][0]["roles"][0]["resources"]["disk"]
    host_port = _unused_port()
    identity = f"{os.getpid()}-{host_port}"
    image = f"localhost/vonk/dev-http-smoke-acceptance:{identity}"
    container = f"vonk-dev-http-smoke-{identity}"
    archive = tmp_path / "image.oci.tar"

    try:
        subprocess.run(
            [
                podman,
                "build",
                "--no-cache",
                "--network=none",
                "--platform=linux/arm64",
                "--tag",
                image,
                str(CONTEXT_ROOT),
            ],
            check=True,
            timeout=300,
        )
        subprocess.run(
            [
                podman,
                "save",
                "--format=oci-archive",
                "--output",
                str(archive),
                image,
            ],
            check=True,
            timeout=120,
        )
        assert 0 < archive.stat().st_size <= disk["image_bytes"]

        subprocess.run([podman, "image", "rm", image], check=True, timeout=60)
        subprocess.run(
            [podman, "load", "--input", str(archive)], check=True, timeout=120
        )
        subprocess.run(
            [
                podman,
                "run",
                "--detach",
                "--name",
                container,
                "--restart=no",
                "--read-only",
                "--cap-drop=ALL",
                "--security-opt=no-new-privileges",
                "--userns=keep-id:uid=10001,gid=10001",
                "--network=slirp4netns:allow_host_loopback=false",
                "--user=10001:10001",
                "--publish",
                f"127.0.0.1:{host_port}:8000",
                "--env",
                "VONK_LISTEN_HOST=0.0.0.0",
                "--env",
                "VONK_LISTEN_PORT=8000",
                "--env",
                "PYTHONDONTWRITEBYTECODE=1",
                image,
                "python",
                "/app/server.py",
            ],
            check=True,
            timeout=60,
        )

        deadline = time.monotonic() + 30
        while True:
            try:
                status, payload = _request_json(
                    "127.0.0.1", host_port, "GET", "/health"
                )
            except OSError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.2)
                continue
            assert status == 200
            assert payload == fixture_expected()["health"]
            break
    finally:
        subprocess.run(
            [podman, "rm", "--force", container],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=30,
        )
        subprocess.run(
            [podman, "image", "rm", "--force", image],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=30,
        )
