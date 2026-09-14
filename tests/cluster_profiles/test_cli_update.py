from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import shutil
import subprocess
import time
import zipfile
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from cluster_profiles import cli, cli_update


def _signed_publication(
    tmp_path: Path,
    *,
    source_sha: str,
    wheel: bytes | None = None,
    omit_images: bool = False,
) -> tuple[Path, dict[str, bytes]]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
    public_key = tmp_path / "installer-public.pem"
    public_key.write_bytes(
        key.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        )
    )
    wheel_buffer = io.BytesIO()
    with zipfile.ZipFile(wheel_buffer, "w") as archive:
        archive.writestr(
            "cluster_profiles/build-identity.json",
            json.dumps(
                {
                    "schema_version": 1,
                    "source_sha": source_sha,
                    "release_version": "1.2.3",
                }
            ),
        )
    wheel = wheel if wheel is not None else wheel_buffer.getvalue()
    generation = "a" * 64
    prefix = f"artifacts/stable/releases/{generation}"
    wheel_path = f"{prefix}/cli/vonk_cluster_profiles-0.1.1-py3-none-any.whl"
    descriptor = {"path": f"{prefix}/example", "sha256": "a" * 64, "size": 1}
    artifacts = {
        name: dict(descriptor)
        for name in (
            "agent-package-signature-linux-arm64",
            "spark-setup-linux-arm64",
            "spark-setup-signature-linux-arm64",
            "nas-payload",
            "nas-setup-darwin-amd64",
            "nas-setup-darwin-arm64",
            "nas-setup-linux-amd64",
            "nas-setup-linux-arm64",
        )
    }
    artifacts["agent-package-linux-arm64"] = {
        **descriptor,
        "architecture": "linux-arm64",
        "host_signature": "a" * 128,
        "package_version": "1.2.3",
        "target_binary_digest": "a" * 64,
        "target_build_digest": "sha256:" + "a" * 64,
    }
    artifacts["cli-wheel"] = {
        "path": wheel_path,
        "sha256": hashlib.sha256(wheel).hexdigest(),
        "size": len(wheel),
    }
    release = {
        "schema_version": 2,
        "channel": "stable",
        "generation": generation,
        "version": "1.2.3",
        "source_sha": source_sha,
        "images": {
            name: "ghcr.io/vonk/" + name + ":v1@sha256:" + "a" * 64
            for name in ("api", "worker", "hermes", "litellm")
        },
        "artifacts": artifacts,
        "bootstraps": {"nas": dict(descriptor), "spark": dict(descriptor)},
    }
    if omit_images:
        del release["images"]
    release_raw = (
        json.dumps(release, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode()
    release_sig = (
        base64.b64encode(key.sign(release_raw, padding.PKCS1v15(), hashes.SHA256()))
        + b"\n"
    )
    claims = (
        "schema_version=2\nchannel=stable\n"
        f"generation={generation}\nversion=1.2.3\nsource_sha={source_sha}\n"
        f"expires_at={int(time.time()) + 3600}\n"
        f"release_path={prefix}/release.json\n"
        f"release_sha256={hashlib.sha256(release_raw).hexdigest()}\n"
        f"release_signature_path={prefix}/release.sig\n"
        f"release_signature_sha256={hashlib.sha256(release_sig).hexdigest()}\n"
        "nas_path=unused\nnas_sha256=unused\nspark_path=unused\nspark_sha256=unused\n"
    ).encode()
    pointer = (
        claims
        + b"signature="
        + base64.b64encode(key.sign(claims, padding.PKCS1v15(), hashes.SHA256()))
        + b"\n"
    )
    return public_key, {
        "https://install.vonkforge.ai/artifacts/stable/current.manifest": pointer,
        f"https://install.vonkforge.ai/{prefix}/release.json": release_raw,
        f"https://install.vonkforge.ai/{prefix}/release.sig": release_sig,
        f"https://install.vonkforge.ai/{wheel_path}": wheel,
    }


def test_version_is_offline_and_does_not_construct_controller(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        cli.ControlClient,
        "from_environment",
        lambda: pytest.fail("Controller accessed"),
    )
    output = StringIO()
    with redirect_stdout(output):
        status = cli.main(("--json", "--version"))
    assert status == 0
    assert "version" in json.loads(output.getvalue())


def test_update_dispatches_before_controller_authentication(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        cli.ControlClient,
        "from_environment",
        lambda: pytest.fail("Controller accessed"),
    )
    monkeypatch.setattr(
        cli,
        "run_update",
        lambda **kwargs: {"updated": False, "update_available": False},
    )
    output = StringIO()
    with redirect_stdout(output):
        status = cli.main(
            ("update", "--public-key", str(tmp_path / "key.pem"), "--json")
        )
    assert status == 0
    assert json.loads(output.getvalue()) == {
        "updated": False,
        "update_available": False,
    }


def test_update_installs_only_changed_signed_wheel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_sha = "b" * 40
    key, objects = _signed_publication(tmp_path, source_sha=source_sha)
    seen: list[list[str]] = []
    monkeypatch.setattr(
        cli_update,
        "current_build",
        lambda: {"version": "0.1.1", "source_sha": "c" * 40},
    )

    def install(command, **kwargs):
        seen.append(command)
        assert "--no-deps" in command and "--offline" in command
        assert command[1:3] == ["pip", "install"]
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(cli_update.subprocess, "run", install)
    download = lambda url, maximum: objects[url]
    checked = cli_update.run_update(
        channel="stable",
        public_key=key,
        origin="https://install.vonkforge.ai",
        apply=False,
        download=download,
    )
    assert checked["update_available"] is True and not seen
    applied = cli_update.run_update(
        channel="stable",
        public_key=key,
        origin="https://install.vonkforge.ai",
        apply=True,
        download=download,
    )
    assert applied["updated"] is True and len(seen) == 1

    monkeypatch.setattr(
        cli_update,
        "current_build",
        lambda: {"version": "1.2.3", "source_sha": source_sha},
    )
    unchanged = cli_update.run_update(
        channel="stable",
        public_key=key,
        origin="https://install.vonkforge.ai",
        apply=True,
        download=download,
    )
    assert unchanged["updated"] is False and len(seen) == 1


def test_update_rejects_tampered_release_before_wheel_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    key, objects = _signed_publication(tmp_path, source_sha="b" * 40)
    pointer_url = "https://install.vonkforge.ai/artifacts/stable/current.manifest"
    objects[pointer_url] = objects[pointer_url].replace(
        b"channel=stable", b"channel=dev"
    )
    monkeypatch.setattr(
        cli_update.subprocess,
        "run",
        lambda *args, **kwargs: pytest.fail("wheel installed"),
    )
    with pytest.raises(cli_update.CliUpdateError, match="signature"):
        cli_update.run_update(
            channel="stable",
            public_key=key,
            origin="https://install.vonkforge.ai",
            apply=True,
            download=lambda url, maximum: objects[url],
        )


def test_update_rejects_signed_release_missing_required_current_fields(
    tmp_path: Path,
) -> None:
    key, objects = _signed_publication(tmp_path, source_sha="b" * 40, omit_images=True)
    with pytest.raises(cli_update.CliUpdateError, match="schema|invalid"):
        cli_update.run_update(
            channel="stable",
            public_key=key,
            origin="https://install.vonkforge.ai",
            apply=False,
            download=lambda url, maximum: objects[url],
        )


@pytest.mark.lane
def test_signed_update_installs_real_wheel_into_uv_venv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An accepted wheel must replace the CLI inside uv's pip-free venv."""
    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("uv is unavailable")
    root = Path(__file__).resolve().parents[2]

    def build(directory: Path, source: str, version: str) -> Path:
        environment = {
            **os.environ,
            "VONK_BUILD_SOURCE_SHA": source,
            "VONK_BUILD_RELEASE_VERSION": version,
        }
        subprocess.run(
            [uv, "build", "--wheel", "--offline", "--out-dir", str(directory)],
            cwd=root,
            env=environment,
            check=True,
            capture_output=True,
            text=True,
        )
        wheels = list(directory.glob("vonk_cluster_profiles-*-py3-none-any.whl"))
        assert len(wheels) == 1
        return wheels[0]

    old_wheel = build(tmp_path / "old", "c" * 40, "0.1.1")
    accepted_wheel = build(tmp_path / "accepted", "b" * 40, "1.2.3")
    environment = tmp_path / "cli-env"
    subprocess.run(
        [uv, "venv", "--python", "3.12", str(environment)],
        check=True,
        capture_output=True,
        text=True,
    )
    python = environment / "bin" / "python"
    installed_environment = {**os.environ, "PYTHONPATH": ""}
    subprocess.run(
        [uv, "pip", "install", "--offline", "--python", str(python), str(old_wheel)],
        check=True,
        capture_output=True,
        text=True,
    )
    assert (
        subprocess.run(
            [str(python), "-m", "pip", "--version"], capture_output=True, check=False
        ).returncode
        != 0
    )
    before = subprocess.run(
        [str(environment / "bin" / "vonkctl"), "--json", "--version"],
        env=installed_environment,
        check=True,
        capture_output=True,
        text=True,
    )
    assert json.loads(before.stdout)["source_sha"] == "c" * 40

    key, objects = _signed_publication(
        tmp_path, source_sha="b" * 40, wheel=accepted_wheel.read_bytes()
    )
    monkeypatch.setattr(
        cli_update,
        "current_build",
        lambda: {"version": "0.1.1", "source_sha": "c" * 40},
    )
    monkeypatch.setattr(cli_update.sys, "executable", str(python))
    result = cli_update.run_update(
        channel="stable",
        public_key=key,
        origin="https://install.vonkforge.ai",
        apply=True,
        download=lambda url, maximum: objects[url],
    )
    assert result["updated"] is True
    after = subprocess.run(
        [str(environment / "bin" / "vonkctl"), "--json", "--version"],
        env=installed_environment,
        check=True,
        capture_output=True,
        text=True,
    )
    assert json.loads(after.stdout) == {"version": "1.2.3", "source_sha": "b" * 40}


def test_interactive_notice_never_fetches_on_ordinary_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("VONK_CLI_UPDATE_NOTICES", "1")
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    monkeypatch.setattr(
        cli_update,
        "run_update",
        lambda **kwargs: pytest.fail("network check blocked ordinary command"),
    )
    assert cli_update.interactive_notice() is None
    cache = tmp_path / "vonkctl" / "update-notice.json"
    cache.parent.mkdir()
    cache.write_text(
        json.dumps(
            {
                "checked_at": int(time.time()),
                "update_available": True,
                "source_sha": cli_update.current_build()["source_sha"],
            }
        )
    )
    assert cli_update.interactive_notice() is not None
