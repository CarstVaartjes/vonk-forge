from __future__ import annotations

import base64
import hashlib
import hmac
import json
import stat
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/dev-admin-token"
DEVELOPMENT_KEY = b"runtime-generated-development-key"


def _payload(token: str) -> dict[str, object]:
    body, signature = token.split(".", 1)
    expected = hmac.new(DEVELOPMENT_KEY, body.encode(), hashlib.sha256).digest()
    observed = base64.urlsafe_b64decode(signature + "=" * (-len(signature) % 4))
    assert hmac.compare_digest(observed, expected)
    decoded = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4))
    return json.loads(decoded)


def test_helper_writes_a_private_short_lived_development_admin_token(
    tmp_path: Path,
) -> None:
    output = tmp_path / "admin-token"
    signing_key = tmp_path / "token-signing-key"
    signing_key.write_bytes(DEVELOPMENT_KEY + b"\n")
    signing_key.chmod(0o600)

    result = subprocess.run(
        (
            sys.executable,
            str(SCRIPT),
            "--output",
            str(output),
            "--signing-key-file",
            str(signing_key),
            "--ttl-seconds",
            "600",
        ),
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == f"{output.resolve()}\n"
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    token = output.read_text(encoding="ascii").strip()
    payload = _payload(token)
    assert payload["sub"] == "development-operator"
    assert payload["role"] == "administrator"
    assert int(time.time()) < payload["exp"] <= int(time.time()) + 600
    assert token not in result.stdout + result.stderr


def test_helper_refuses_to_replace_an_existing_token(tmp_path: Path) -> None:
    output = tmp_path / "admin-token"
    output.write_text("preserve-me\n", encoding="ascii")
    output.chmod(0o600)
    signing_key = tmp_path / "token-signing-key"
    signing_key.write_bytes(DEVELOPMENT_KEY + b"\n")
    signing_key.chmod(0o600)

    result = subprocess.run(
        (
            sys.executable,
            str(SCRIPT),
            "--output",
            str(output),
            "--signing-key-file",
            str(signing_key),
        ),
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 1
    assert output.read_text(encoding="ascii") == "preserve-me\n"
    assert "already exists" in result.stderr


def test_helper_rejects_an_insecure_signing_key_file(tmp_path: Path) -> None:
    output = tmp_path / "admin-token"
    signing_key = tmp_path / "token-signing-key"
    signing_key.write_bytes(DEVELOPMENT_KEY + b"\n")
    signing_key.chmod(0o644)

    result = subprocess.run(
        (
            sys.executable,
            str(SCRIPT),
            "--output",
            str(output),
            "--signing-key-file",
            str(signing_key),
        ),
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 1
    assert not output.exists()
    assert "signing key" in result.stderr
