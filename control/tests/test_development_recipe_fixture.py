from __future__ import annotations

import hashlib
import json
import os
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from http.client import HTTPConnection
from pathlib import Path

from vonk_control.recipe_contract import recipe_content_sha256, validate_recipe
from vonk_control.source_bundles import generate_source_bundle
from vonk_control.source_policy import enforce_build_source_policy

FIXTURE_ROOT = Path(__file__).parent / "fixtures/recipes/dev-http-smoke"
CONTEXT_ROOT = FIXTURE_ROOT / "context"
PROJECT_ROOT = Path(__file__).resolve().parents[2]
EXPECTED_SOURCE_SHA256 = (
    "7a65752ee1a950b3b358c66ceaf2007d0eb824a7842d0a67a5b1e3726957eb80"
)
EXPECTED_RECIPE_SHA256 = (
    "11fca06a786d0a84d2b5fe34a2ae4423327f1b79e6a3096b93714e494130842a"
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
EXPECTED_TOTAL_BYTES = 2469
EXPECTED_FILE_SHA256 = {
    "Dockerfile": "701a82b82b5056c17eb3dcf1e6c4d281e359ad771bc29a84c94715327e8f8257",
    "server.py": "be0189b161a86bf72d8c1a9d93e03a724810c1488b5cdaaedd773ff0aea118e8",
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
    port: int, method: str, path: str, payload: dict[str, object] | None = None
) -> tuple[int, object]:
    connection = HTTPConnection("127.0.0.1", port, timeout=2)
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


def _unused_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        return int(listener.getsockname()[1])


@contextmanager
def running_fixture_server() -> Iterator[int]:
    port = _unused_port()
    process = subprocess.Popen(
        [sys.executable, str(CONTEXT_ROOT / "server.py")],
        env={**os.environ, "PORT": str(port)},
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
                status, payload = _request_json(port, "GET", "/health")
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


def test_dev_http_smoke_server_matches_expected_health_and_inference() -> None:
    recipe = fixture_recipe()
    expected = fixture_expected()

    with running_fixture_server() as port:
        status, health = _request_json(
            port, "GET", str(recipe["runtime"]["endpoint"]["health_path"])
        )
        assert status == 200
        assert health == expected["health"]

        status, response = _request_json(
            port, "POST", "/v1/chat/completions", expected["request"]
        )
        assert status == 200
        assert response == expected["response"]
