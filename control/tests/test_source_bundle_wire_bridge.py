from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest
from vonk_control.source_bundles import generate_source_bundle

from tests.wire_probes import prebuilt_probe


@pytest.fixture(scope="session")
def source_bundle_probe() -> Path:
    return prebuilt_probe("VONK_SOURCE_BUNDLE_WIRE_PROBE")


def test_python_source_bundle_digest_and_bytes_reach_rust_materializer(
    source_bundle_probe,
):
    files = {
        "Dockerfile": b"FROM scratch\nUSER 10001\n",
        "nested/empty": b"",
        "nested/café.txt": "héllo\n".encode(),
    }
    bundle = generate_source_bundle(files)
    # Captured from the previous current producer, before replacing its DTOs.
    assert (
        bundle.sha256
        == "f7c1117c22682d4d1a094b7829a8d7d217d333de6e61e08e225dcb5af6849fbb"
    )
    assert (
        hashlib.sha256(bundle.archive).hexdigest()
        == "929f0b528dfb33b3de12831076e72661d0188fd79bc0e6eee3bb9d9053a27902"
    )
    result = subprocess.run(
        [str(source_bundle_probe), bundle.sha256],
        input=bundle.archive,
        capture_output=True,
        check=True,
    )
    assert {
        path: bytes(content) for path, content in json.loads(result.stdout).items()
    } == files
    substituted = subprocess.run(
        [str(source_bundle_probe), "f" * 64],
        input=bundle.archive,
        capture_output=True,
        check=False,
    )
    assert substituted.returncode != 0
