from __future__ import annotations

import base64
import hashlib
import os
import subprocess
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _sign(private_key: Path, source: Path, destination: Path) -> None:
    raw = destination.with_suffix(".raw")
    subprocess.run(
        ["openssl", "dgst", "-sha256", "-sign", private_key, "-out", raw, source],
        check=True,
        capture_output=True,
    )
    destination.write_bytes(base64.b64encode(raw.read_bytes()) + b"\n")


@pytest.mark.parametrize(
    ("kind", "arguments"),
    (
        ("spark", ("--enroll",)),
        ("nas", ("--answers-file", "answers.txt", "--disable-hermes")),
    ),
)
def test_channel_passes_the_verified_immutable_release_to_setup(
    tmp_path: Path, kind: str, arguments: tuple[str, ...]
) -> None:
    private_key = tmp_path / "private.pem"
    public_key = tmp_path / "public.pem"
    subprocess.run(
        [
            "openssl",
            "genpkey",
            "-algorithm",
            "RSA",
            "-pkeyopt",
            "rsa_keygen_bits:2048",
            "-out",
            private_key,
        ],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["openssl", "pkey", "-in", private_key, "-pubout", "-out", public_key],
        check=True,
        capture_output=True,
    )
    public = tmp_path / "public"
    generation = "a" * 64
    prefix = Path("artifacts/stable/releases") / generation
    release = public / prefix / "release.json"
    signature = public / prefix / "release.sig"
    spark = public / prefix / "bootstraps/spark"
    nas = public / prefix / "bootstraps/nas"
    for path in (release, signature, spark, nas):
        path.parent.mkdir(parents=True, exist_ok=True)
    release.write_text('{"schema_version":2,"signed":"immutable"}\n')
    _sign(private_key, release, signature)
    receipt = tmp_path / "receipt"
    selected = {"nas": nas, "spark": spark}[kind]
    unselected = {"nas": spark, "spark": nas}[kind]
    selected.write_text(
        "#!/bin/sh\nset -eu\n"
        'cat "$VONK_INSTALL_RELEASE_MANIFEST" > "$VONK_TEST_RECEIPT"\n'
        'cat "$VONK_INSTALL_RELEASE_SIGNATURE" >> "$VONK_TEST_RECEIPT"\n'
        'printf "%s\\n" "$VONK_INSTALL_BASE_URL" >> "$VONK_TEST_RECEIPT"\n'
        'printf "%s\\n" "$VONK_CONTROLLER_ADDRESS" >> "$VONK_TEST_RECEIPT"\n'
        'printf "arguments=%s\\n" "$*" >> "$VONK_TEST_RECEIPT"\n'
    )
    unselected.write_text("unused\n")
    claims = public / "artifacts/stable/current.claims"
    claims.parent.mkdir(parents=True, exist_ok=True)
    claims.write_text(
        "schema_version=2\n"
        "channel=stable\n"
        f"generation={generation}\n"
        "version=1.0.0\n"
        f"source_sha={'b' * 40}\n"
        f"expires_at={int(time.time()) + 600}\n"
        f"release_path={prefix}/release.json\n"
        f"release_sha256={hashlib.sha256(release.read_bytes()).hexdigest()}\n"
        f"release_signature_path={prefix}/release.sig\n"
        f"release_signature_sha256={hashlib.sha256(signature.read_bytes()).hexdigest()}\n"
        f"nas_path={prefix}/bootstraps/nas\n"
        f"nas_sha256={hashlib.sha256(nas.read_bytes()).hexdigest()}\n"
        f"spark_path={prefix}/bootstraps/spark\n"
        f"spark_sha256={hashlib.sha256(spark.read_bytes()).hexdigest()}\n"
    )
    pointer_signature = tmp_path / "pointer.sig"
    _sign(private_key, claims, pointer_signature)
    (public / "artifacts/stable/current.manifest").write_bytes(
        claims.read_bytes()
        + b"signature="
        + pointer_signature.read_bytes().strip()
        + b"\n"
    )
    rendered = tmp_path / kind
    rendered.write_text(
        (ROOT / "install/channel")
        .read_text()
        .replace("@VONK_KIND@", kind)
        .replace("@VONK_CHANNEL@", "stable")
        .replace("@VONK_ORIGIN@", "https://install.example.test")
        .replace("@VONK_PUBLIC_KEY_PEM@", public_key.read_text())
    )
    commands = tmp_path / "commands"
    commands.mkdir()
    curl = commands / "curl"
    curl.write_text(
        "#!/bin/sh\nset -eu\ndestination=\nurl=\n"
        'while [ "$#" -gt 0 ]; do case "$1" in -o) destination=$2; shift 2;; -*) shift;; *) url=$1; shift;; esac; done\n'
        'cp "$VONK_TEST_PUBLIC/${url#https://install.example.test/}" "$destination"\n'
    )
    curl.chmod(0o700)

    result = subprocess.run(
        ["sh", rendered, *arguments],
        cwd=tmp_path,
        env={
            **os.environ,
            "PATH": f"{commands}:{os.environ['PATH']}",
            "VONK_TEST_PUBLIC": str(public),
            "VONK_TEST_RECEIPT": str(receipt),
            "VONK_CONTROLLER_ADDRESS": "192.168.1.231",
        },
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert receipt.read_bytes() == (
        release.read_bytes()
        + signature.read_bytes()
        + f"https://install.example.test/{prefix}\n".encode()
        + b"192.168.1.231\n"
        + f"arguments={' '.join(arguments)}\n".encode()
    )
