from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import shutil
import ssl
import subprocess
import sys
import time
import zipfile
from contextlib import redirect_stdout
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import StringIO
from pathlib import Path
from threading import Thread

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.x509.oid import NameOID

from cluster_profiles import cli, cli_update
from cluster_profiles.runtime_identity import contract_fingerprint


def _control_schema():
    return json.loads(
        (
            Path(__file__).resolve().parents[2]
            / "src/cluster_profiles/schemas/control-openapi.json"
        ).read_text()
    )


def _compatibility_schema():
    return json.loads(
        (
            Path(__file__).resolve().parents[2]
            / "src/cluster_profiles/schemas/cli-update-contract.schema.json"
        ).read_text()
    )


def _controller_observation() -> dict[str, object]:
    return {
        "observed_at": "2026-10-07T00:00:00Z",
        "compatibility_schema_sha256": contract_fingerprint(_compatibility_schema()),
        "api": {
            "source_sha": "a" * 40,
            "control_contract_sha256": contract_fingerprint(_control_schema()),
        },
        "expected_worker_contract_sha256": "d" * 64,
        "worker_count": 1,
        "worker_membership_sha256": "e" * 64,
        "worker_source_sha": "f" * 40,
        "worker_contract_sha256": "d" * 64,
        "worker_compatibility": "compatible",
        "worker_issue": None,
    }


@pytest.fixture(autouse=True)
def isolated_update_channel(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("VONK_CLI_UPDATE_CHANNEL", raising=False)


def test_update_download_identifies_product_without_following_redirects() -> None:
    requests: list[str] = []

    class ReleaseHost(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            requests.append(self.path)
            if not self.headers.get("User-Agent", "").startswith("vonkctl/"):
                self.send_response(403)
            elif self.path == "/redirect":
                self.send_response(302)
                self.send_header("Location", "/unexpected")
            else:
                self.send_response(200)
            self.end_headers()
            self.wfile.write(b"release")

        def log_message(self, *_args) -> None:
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), ReleaseHost) as server:
        worker = Thread(target=server.serve_forever, daemon=True)
        worker.start()
        origin = f"http://127.0.0.1:{server.server_port}"
        try:
            assert cli_update._download(f"{origin}/current.manifest", 7) == b"release"
            with pytest.raises(cli_update.CliUpdateError, match="too large"):
                cli_update._download(f"{origin}/oversized", 6)
            with pytest.raises(cli_update.CliUpdateError, match="redirected"):
                cli_update._download(f"{origin}/redirect", 7)
            assert "/unexpected" not in requests
        finally:
            server.shutdown()
            worker.join(timeout=5)


def _signed_publication(
    tmp_path: Path,
    *,
    source_sha: str,
    channel: str = "stable",
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
                    "schema_version": 2,
                    "control_contract_sha256": contract_fingerprint(_control_schema()),
                    "worker_contract_sha256": "d" * 64,
                    "source_sha": source_sha,
                    "release_version": "1.2.3",
                }
            ),
        )
        archive.writestr(
            "cluster_profiles/schemas/control-openapi.json",
            json.dumps(_control_schema()),
        )
        archive.writestr(
            "cluster_profiles/schemas/cli-update-contract.schema.json",
            json.dumps(_compatibility_schema()),
        )
    wheel = wheel if wheel is not None else wheel_buffer.getvalue()
    generation = "a" * 64
    prefix = f"artifacts/{channel}/releases/{generation}"
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
        "channel": channel,
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
        f"schema_version=2\nchannel={channel}\n"
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
        f"https://install.vonkforge.ai/artifacts/{channel}/current.manifest": pointer,
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
    monkeypatch.setenv("VONK_CLI_UPDATE_CHANNEL", "dev")
    monkeypatch.setattr(
        cli.ControlClient,
        "from_environment",
        lambda: pytest.fail("Controller accessed"),
    )
    selected: list[str] = []

    def check(**kwargs):
        selected.append(kwargs["channel"])
        return {"updated": False, "update_available": False}

    monkeypatch.setattr(cli, "run_update", check)
    output = StringIO()
    with redirect_stdout(output):
        status = cli.main(("update", "--json"))
    assert status == 0
    assert selected == ["dev"]
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
        # Installed as a uv tool, so the release's dependencies resolve and
        # the tool receipt follows the new wheel.
        assert command[1:6] == ["tool", "install", "--force", "--python", "3.14"]
        assert command[-1].endswith(".whl")
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
        compatibility_observation=_controller_observation,
    )
    assert applied["updated"] is True and len(seen) == 1
    assert applied["previous"] == checked["current"]
    assert applied["current"] == {"version": "1.2.3", "source_sha": source_sha}
    assert applied["update_available"] is False

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
        compatibility_observation=_controller_observation,
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


def test_interactive_notice_never_fetches_on_ordinary_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    key, _ = _signed_publication(tmp_path, source_sha="b" * 40)
    monkeypatch.setattr(cli_update, "INSTALLER_PUBLIC_KEY", key)
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
                "verified": True,
                "channel": "stable",
                "origin": "https://install.vonkforge.ai",
                "update_available": True,
                "source_sha": cli_update.current_build()["source_sha"],
                "version": cli_update.current_build()["version"],
                "key_sha256": hashlib.sha256(key.read_bytes()).hexdigest(),
            }
        )
    )
    assert cli_update.interactive_notice() is not None
    (tmp_path / "other").mkdir()
    other_key, _ = _signed_publication(tmp_path / "other", source_sha="d" * 40)
    monkeypatch.setattr(cli_update, "INSTALLER_PUBLIC_KEY", other_key)
    assert cli_update.interactive_notice() is None


def test_interactive_command_schedules_without_blocking_controller(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        cli, "begin_interactive_update_check", cli_update.begin_interactive_update_check
    )
    key, _ = _signed_publication(tmp_path, source_sha="b" * 40)
    monkeypatch.setattr(cli_update, "INSTALLER_PUBLIC_KEY", key)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    seen: list[list[str]] = []
    monkeypatch.setattr(
        cli_update, "run_update", lambda **kwargs: pytest.fail("network blocked CLI")
    )
    monkeypatch.setattr(
        cli_update.subprocess,
        "Popen",
        lambda command, **kwargs: seen.append(command),
    )
    monkeypatch.setattr(cli, "run_controller", lambda *args: {"state": "ready"})
    monkeypatch.setattr(cli, "_emit", lambda *args: None)

    class InteractiveError(StringIO):
        def isatty(self) -> bool:
            return True

    monkeypatch.setattr(cli.sys, "stderr", InteractiveError())
    monkeypatch.setattr(cli.sys, "stdin", InteractiveError())
    assert cli.main(("profile", "list"), control_client=object()) == 0
    assert len(seen) == 1
    assert seen[0][1:4] == ["-m", "cluster_profiles.cli_update", "--background-notice"]
    assert cli.main(("profile", "list"), control_client=object()) == 0
    assert len(seen) == 1
    lock = tmp_path / "vonkctl" / "update-notice.lock"
    stale = time.time() - 31
    os.utime(lock, (stale, stale))
    assert cli.main(("profile", "list"), control_client=object()) == 0
    assert len(seen) == 2
    lock.unlink()

    def failed_spawn(command, **kwargs):
        raise OSError("background process unavailable")

    monkeypatch.setattr(cli_update.subprocess, "Popen", failed_spawn)
    assert cli.main(("profile", "list"), control_client=object()) == 0
    assert not lock.exists()


def test_background_notice_accepts_only_signed_current_release(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    key, objects = _signed_publication(tmp_path, source_sha="b" * 40)
    monkeypatch.setattr(cli_update, "INSTALLER_PUBLIC_KEY", key)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    monkeypatch.setattr(
        cli_update,
        "current_build",
        lambda: {"version": "0.1.1", "source_sha": "c" * 40},
    )
    observed: list[str] = []

    def download(url: str, maximum: int) -> bytes:
        observed.append(url)
        return objects[url]

    cli_update.background_notice_check(download=download)
    assert cli_update.interactive_notice() is not None
    assert not any(url.endswith(".whl") for url in observed)

    pointer = "https://install.vonkforge.ai/artifacts/stable/current.manifest"
    objects[pointer] = objects[pointer].replace(b"channel=stable", b"channel=dev")
    cli_update.background_notice_check(download=download)
    assert cli_update.interactive_notice() is None
    seen: list[list[str]] = []
    monkeypatch.setattr(
        cli_update.subprocess,
        "Popen",
        lambda command, **kwargs: seen.append(command),
    )
    cli_update.begin_interactive_update_check()
    assert not seen
    cache = tmp_path / "vonkctl" / "update-notice.json"
    failed = json.loads(cache.read_text())
    failed["checked_at"] -= 901
    cache.write_text(json.dumps(failed))
    cli_update.begin_interactive_update_check()
    assert len(seen) == 1


def test_background_module_checks_configured_channel_and_releases_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    key, objects = _signed_publication(tmp_path, source_sha="b" * 40, channel="dev")
    monkeypatch.setenv("VONK_CLI_UPDATE_CHANNEL", "dev")
    monkeypatch.setattr(cli_update, "INSTALLER_PUBLIC_KEY", key)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    assert cli_update.interactive_notice() is None
    lock = tmp_path / "cache" / "vonkctl" / "update-notice.lock"
    lock.parent.mkdir(parents=True)
    lock.touch()
    monkeypatch.setattr(
        cli_update, "_download", lambda url, maximum, timeout: objects[url]
    )
    monkeypatch.setattr(
        cli_update.sys, "argv", ["cli_update.py", "--background-notice"]
    )
    assert cli_update._background_main() == 0
    assert not lock.exists()
    assert cli_update.interactive_notice() == (
        "Accepted vonkctl update available; run "
        "'vonkctl update --channel dev --apply' to install it."
    )
    monkeypatch.setenv("VONK_CLI_UPDATE_CHANNEL", "stable")
    assert cli_update.interactive_notice() is None


def test_offline_version_and_json_command_do_not_schedule_notices(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    key, _ = _signed_publication(tmp_path, source_sha="b" * 40)
    monkeypatch.setattr(cli_update, "INSTALLER_PUBLIC_KEY", key)
    monkeypatch.setattr(
        cli, "begin_interactive_update_check", lambda: pytest.fail("scheduled check")
    )
    monkeypatch.setattr(cli, "run_controller", lambda *args: {"state": "ready"})
    monkeypatch.setattr(cli, "_emit", lambda *args: None)

    class InteractiveError(StringIO):
        def isatty(self) -> bool:
            return True

    monkeypatch.setattr(cli.sys, "stderr", InteractiveError())
    assert cli.main(("--version",)) == 0
    assert cli.main(("--json", "profile", "list"), control_client=object()) == 0


def _install_cli_wheel(uv: str, venv: Path, wheel: Path) -> Path:
    """Install a built CLI wheel with --no-deps and link its locked dependencies.

    scripts/sync-cli-dependencies prepares the CLI's own dependency
    environment; the test never resolves or downloads a dependency.
    """

    root = Path(__file__).resolve().parents[2]
    from tools import cli_dependencies

    found, reason = cli_dependencies.site_packages(root)
    if found is None:
        message = f"the CLI dependency environment is unusable: {reason}"
        if os.environ.get("CI", "").lower() == "true":
            pytest.fail(message, pytrace=False)
        pytest.skip(message)
    dependencies = [found]
    subprocess.run(
        [uv, "venv", "--python", sys.executable, str(venv)],
        check=True,
        capture_output=True,
        text=True,
    )
    python = venv / "bin" / "python"
    installed = subprocess.run(
        [
            uv,
            "pip",
            "install",
            "--offline",
            "--no-deps",
            "--python",
            str(python),
            str(wheel),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert installed.returncode == 0, installed.stderr
    [site_packages] = venv.glob("lib/python3.*/site-packages")
    (site_packages / "vonk-cli-dependencies.pth").write_text(
        f"{dependencies[0]}\n", encoding="utf-8"
    )
    return python


@pytest.mark.parametrize(
    "api",
    [
        {"source_sha": None, "control_contract_sha256": None},
        {"source_sha": "a" * 40, "control_contract_sha256": "0" * 64},
    ],
)
def test_signed_client_update_retains_install_until_deployed_contract_matches(
    tmp_path, monkeypatch, api
) -> None:
    key, objects = _signed_publication(tmp_path, source_sha="b" * 40)
    current = {"version": "0.1.1", "source_sha": "c" * 40}
    monkeypatch.setattr(cli_update, "current_build", lambda: current)
    monkeypatch.setattr(
        cli_update.subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail("incompatible signed client installed"),
    )
    result = cli_update.run_update(
        channel="stable",
        public_key=key,
        origin="https://install.vonkforge.ai",
        apply=True,
        download=lambda url, _maximum: objects[url],
        compatibility_observation=lambda: {**_controller_observation(), "api": api},
    )
    assert result["updated"] is False
    assert result["current"] == current
    assert result["accepted_source_sha"] == "b" * 40
    assert result["compatibility"] == "controller-contract-unavailable-or-different"


@pytest.mark.parametrize(
    "change",
    [
        {
            "worker_compatibility": "unknown",
            "worker_issue": "worker-source-mixed",
            "worker_source_sha": None,
        },
        {"worker_count": 0},
        {"worker_source_sha": None},
        {"expected_worker_contract_sha256": None},
        {"worker_contract_sha256": "0" * 64},
        {"compatibility_schema_sha256": "0" * 64},
    ],
)
def test_update_preserves_installed_tool_for_unknown_or_mixed_complete_workers(
    tmp_path, monkeypatch, change
):
    key, objects = _signed_publication(tmp_path, source_sha="b" * 40)
    current = {"version": "0.1.1", "source_sha": "c" * 40}
    monkeypatch.setattr(cli_update, "current_build", lambda: current)
    monkeypatch.setattr(
        cli_update.subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail("unknown membership installed a wheel"),
    )
    result = cli_update.run_update(
        channel="stable",
        public_key=key,
        origin="https://install.vonkforge.ai",
        apply=True,
        download=lambda url, _maximum: objects[url],
        compatibility_observation=lambda: {**_controller_observation(), **change},
    )
    assert result["updated"] is False
    assert result["current"] == current
    assert result["compatibility"] == "controller-contract-unavailable-or-different"


@pytest.fixture(scope="module")
def signed_update_tool(tmp_path_factory: pytest.TempPathFactory):
    """Build and install once; no test resolves dependencies over the network."""
    if os.environ.get("VONK_SIGNED_UPDATE_PROOF") != "1":
        pytest.skip("run the designated hosted signed CLI update proof lane")
    cache = Path(os.environ["VONK_SIGNED_UPDATE_CACHE"])
    assert cache.is_dir() and any(cache.iterdir()), "locked resolver cache is absent"
    interpreters = Path(os.environ["UV_PYTHON_INSTALL_DIR"])
    assert interpreters.is_dir(), "the hosted managed interpreter directory is absent"
    uv = shutil.which("uv")
    if uv is None or sys.version_info[:2] != (3, 14):
        message = "installed update proof requires uv and Python 3.14"
        if os.environ.get("CI", "").lower() == "true":
            pytest.fail(message, pytrace=False)
        pytest.skip(message)
    workspace = tmp_path_factory.mktemp("signed-cli-tool")
    root = Path(__file__).resolve().parents[2]
    environment = {
        "PATH": os.pathsep.join(
            (str(Path(uv).parent), str(Path(sys.executable).parent), os.defpath)
        ),
        "HOME": str(Path.home()),
        "XDG_CACHE_HOME": str(workspace / "xdg-cache"),
        "XDG_CONFIG_HOME": str(workspace / "xdg-config"),
        "XDG_DATA_HOME": str(workspace / "xdg-data"),
        "PYTHONPATH": "",
        "PYTHONNOUSERSITE": "1",
        "UV_TOOL_DIR": str(workspace / "tools"),
        "UV_TOOL_BIN_DIR": str(workspace / "bin"),
        "UV_OFFLINE": "1",
        "UV_PYTHON_DOWNLOADS": "never",
        "UV_PYTHON_INSTALL_DIR": str(interpreters),
        "VONK_CLI_UPDATE_NOTICES": "0",
        "NO_PROXY": "127.0.0.1,localhost",
        "UV_CACHE_DIR": str(cache),
    }
    wheels = []
    for name, source, version in (
        ("old", "c" * 40, "0.1.1"),
        ("accepted", "b" * 40, "1.2.3"),
    ):
        directory = workspace / name
        built = subprocess.run(
            [
                sys.executable,
                "-m",
                "hatchling",
                "build",
                "--target",
                "wheel",
                "--directory",
                str(directory),
            ],
            cwd=root,
            env={
                **environment,
                "VONK_BUILD_SOURCE_SHA": source,
                "VONK_BUILD_RELEASE_VERSION": version,
            },
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        assert built.returncode == 0, built.stderr
        [wheel] = directory.glob("vonk_cluster_profiles-*-py3-none-any.whl")
        wheels.append(wheel)
    installed = subprocess.run(
        [uv, "tool", "install", "--force", "--python", "3.14", str(wheels[0])],
        env=environment,
        cwd=workspace,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    assert installed.returncode == 0, installed.stderr
    executable = workspace / "bin" / "vonkctl"
    python = workspace / "tools" / "vonk-cluster-profiles" / "bin" / "python"
    assert executable.is_file() and python.is_file()
    return workspace, environment, executable, python, wheels[1]


@pytest.mark.linux_only
@pytest.mark.slow(60)  # Two real updater processes, one actual offline uv replacement.
def test_installed_cli_signed_update_replaces_actual_uv_tool(
    signed_update_tool,
) -> None:
    """Catch verified bytes followed by a fake success or wrong-environment install."""
    import ipaddress

    workspace, environment, executable, python, wheel = signed_update_tool
    key, objects = _signed_publication(
        workspace, source_sha="b" * 40, wheel=wheel.read_bytes()
    )
    tls_key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.now(UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(tls_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(hours=1))
        .add_extension(
            x509.SubjectAlternativeName(
                [x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]
            ),
            critical=False,
        )
        .sign(tls_key, hashes.SHA256())
    )
    cert_path, tls_key_path = workspace / "tls.pem", workspace / "tls-key.pem"
    cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    tls_key_path.write_bytes(
        tls_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    responses = {
        url.removeprefix("https://install.vonkforge.ai"): content
        for url, content in objects.items()
    }
    compatibility = _controller_observation()
    with zipfile.ZipFile(wheel) as archive:
        candidate_identity = json.loads(
            archive.read("cluster_profiles/build-identity.json")
        )
    compatibility["expected_worker_contract_sha256"] = candidate_identity[
        "worker_contract_sha256"
    ]
    compatibility["worker_contract_sha256"] = candidate_identity[
        "worker_contract_sha256"
    ]
    responses["/api/cli/contract"] = json.dumps(compatibility).encode()
    # The Controller has moved its whole-platform transport to NDJSON. The
    # previously installed stable updater must use only its bounded authority.
    responses["/api/platform"] = b'{"kind":"observation-header"}\n'
    token = os.urandom(32).hex()
    token_path = workspace / "test-controller-token"
    token_path.write_text(token)
    token_path.chmod(0o600)
    environment = {**environment, "VONK_CONTROL_TOKEN_FILE": str(token_path)}
    wheel_path = next(path for path in responses if path.endswith(".whl"))
    requests = []

    class PublicationHost(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            requests.append(self.path)
            if (
                self.path == "/api/cli/contract"
                and self.headers.get("Authorization") != f"Bearer {token}"
            ):
                self.send_response(401)
                self.end_headers()
                return
            content = responses.get(self.path)
            self.send_response(200 if content is not None else 404)
            self.send_header(
                "Content-Type",
                "application/x-ndjson"
                if self.path == "/api/platform"
                else "application/json",
            )
            self.end_headers()
            self.wfile.write(content or b"")

        def log_message(self, *_args: object) -> None:
            pass

    def invoke(command: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            command,
            env={**environment, "SSL_CERT_FILE": str(cert_path)},
            cwd=workspace,
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )

    def identity() -> dict:
        observed = invoke([str(executable), "--json", "--version"])
        assert observed.returncode == 0, observed.stderr
        return json.loads(observed.stdout)

    before = identity()
    assert before == {"version": "0.1.1", "source_sha": "c" * 40}
    receipt_path = python.parent.parent / "uv-receipt.toml"
    before_tool_receipt = receipt_path.read_bytes()
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert_path, tls_key_path)
    with ThreadingHTTPServer(("127.0.0.1", 0), PublicationHost) as server:
        server.socket = context.wrap_socket(server.socket, server_side=True)
        worker = Thread(target=server.serve_forever, daemon=True)
        worker.start()
        origin = f"https://127.0.0.1:{server.server_port}"
        environment = {**environment, "VONK_CONTROL_URL": origin}
        # Import from the installed tool, with no repository import path. Both
        # HTTPS verification and run_update's uv subprocess are unmodified.
        driver = (
            "import json,sys; from pathlib import Path; "
            "from cluster_profiles import cli_update; "
            "assert Path(cli_update.__file__).is_relative_to(sys.prefix); "
            "print(json.dumps(cli_update.run_update(channel='stable',apply=True,"
            "origin=sys.argv[1],public_key=Path(sys.argv[2]))))"
        )
        try:
            responses[wheel_path] = b"!" + responses[wheel_path][1:]
            rejected = invoke([str(python), "-c", driver, origin, str(key)])
            assert rejected.returncode != 0
            assert "CLI wheel digest or size is invalid" in rejected.stderr
            assert identity() == before
            assert receipt_path.read_bytes() == before_tool_receipt
            responses[wheel_path] = wheel.read_bytes()
            applied = invoke([str(python), "-c", driver, origin, str(key)])
            assert applied.returncode == 0, applied.stderr
            receipt = json.loads(applied.stdout)
            after = identity()
            assert after == {"version": "1.2.3", "source_sha": "b" * 40}
            assert receipt["updated"] is True
            assert receipt["compatibility"] == "compatible"
            assert receipt["previous"] == before and receipt["current"] == after
            assert requests.count(wheel_path) == 2
            assert requests.count("/api/cli/contract") == 1
            assert "/api/platform" not in requests
            # uv's real tool receipt must now reference the installed accepted
            # bytes rather than retaining the initial fixture wheel.
            tool_receipt = (python.parent.parent / "uv-receipt.toml").read_text()
            assert str(workspace / "old") not in tool_receipt
            assert "vonkctl-update-" in tool_receipt
            if report := os.environ.get("VONK_SIGNED_UPDATE_REPORT"):
                Path(report).write_text(
                    json.dumps(
                        {
                            "before": before,
                            "after": after,
                            "receipt": receipt,
                            "tampered_wheel_retained_prior_tool_receipt": True,
                            "stable_updater_after_platform_ndjson": True,
                            "real_uv_tool_receipt_changed": receipt_path.read_bytes()
                            != before_tool_receipt,
                        },
                        indent=2,
                    )
                    + "\n"
                )
        finally:
            server.shutdown()
            worker.join(timeout=5)
