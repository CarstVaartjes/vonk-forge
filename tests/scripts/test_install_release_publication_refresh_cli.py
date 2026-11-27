"""Install release publication: refresh cli."""

from __future__ import annotations

import base64
import copy
import json
import os
import subprocess
import sys
import time
import zipfile
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from tests.scripts.install_release_publication_support import (
    DEPENDENCIES_EXCLUDE_NEWER,
    ROOT,
    SCRIPT,
    _assemble,
    _canonical,
    _input_path,
    _inputs,
    _publish_accepted,
)
from vonk_agent_protocol.installer_release import InstallerReleaseManifest

pytestmark = pytest.mark.needs_dpkg_deb


def test_refresh_refuses_to_extend_manifest_with_missing_release_object(
    tmp_path: Path,
) -> None:
    inputs = _inputs(tmp_path / "inputs")
    publication = _assemble(tmp_path / "inputs", inputs)
    destination = tmp_path / "public"
    published = _publish_accepted(publication, destination, tmp_path / "acceptance")
    assert published.returncode == 0, published.stderr
    manifest = destination / "artifacts/stable/current.manifest"
    before = manifest.read_bytes()
    release_path = next(
        line.removeprefix("release_path=")
        for line in manifest.read_text().splitlines()
        if line.startswith("release_path=")
    )
    (destination / release_path).unlink()

    refreshed = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "refresh",
            "--channel",
            "stable",
            "--expires-at",
            str(int(time.time()) + 14 * 24 * 60 * 60),
            "--signing-key",
            str(inputs["signing_key"]),
            "--signing-public-key",
            str(inputs["signing_public_key"]),
            "--filesystem",
            str(destination),
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert refreshed.returncode == 2
    assert "release object is unavailable" in refreshed.stderr
    assert manifest.read_bytes() == before


def test_public_nas_endpoint_verifies_signed_manifest_before_running_release(
    tmp_path: Path,
) -> None:
    inputs = _inputs(tmp_path / "inputs")
    receipt = tmp_path / "receipt"
    nas_wheel = Path(str(inputs["nas_wheel"]))
    with zipfile.ZipFile(nas_wheel) as archive:
        wheel_members = {name: archive.read(name) for name in archive.namelist()}
    wheel_members["vonk_nas_setup/cli.py"] = (
        b"import os, sys\nfrom pathlib import Path\n"
        b"Path(os.environ['VONK_TEST_RECEIPT']).write_text(' '.join(sys.argv[1:]) + '\\n' + os.environ.get('VONK_INSTALLER_URL', '') + '\\n')\n"
    )
    with zipfile.ZipFile(nas_wheel, "w") as archive:
        for name, content in wheel_members.items():
            archive.writestr(name, content)
    publication = _assemble(tmp_path / "inputs", inputs)
    destination = tmp_path / "public"
    published = _publish_accepted(publication, destination, tmp_path / "acceptance")
    assert published.returncode == 0, published.stderr
    commands = tmp_path / "commands"
    commands.mkdir()
    curl = commands / "curl"
    curl.write_text(
        "#!/bin/sh\nset -eu\ndestination=\nurl=\n"
        "while [ $# -gt 0 ]; do case $1 in -o) destination=$2; shift 2;; -*) shift;; *) url=$1; shift;; esac; done\n"
        'path=${url#https://install.example.test/}\ncp "$VONK_TEST_PUBLIC/$path" "$destination"\n'
    )
    curl.chmod(0o755)
    uv = commands / "uv"
    uv.write_text(
        '#!/bin/sh\nset -eu\n[ "$1" = run ]; [ "$6" = --with ]; wheel=$7; shift 8\n'
        'exec python3 - "$wheel" "$@" <<\'PYUV\'\n'
        "import sys, zipfile\nwheel = sys.argv.pop(1)\n"
        "with zipfile.ZipFile(wheel) as archive: exec(compile(archive.read('vonk_nas_setup/cli.py'), 'fixture.py', 'exec'))\nPYUV\n"
    )
    uv.chmod(0o755)
    uname = commands / "uname"
    uname.write_text(
        "#!/bin/sh\ncase ${1:-} in -s) echo Linux;; -m) echo x86_64;; esac\n"
    )
    uname.chmod(0o755)
    environment = {
        **os.environ,
        "PATH": f"{commands}:{os.environ['PATH']}",
        "VONK_INSTALL_BASE_URL": "https://install.example.test",
        "VONK_TEST_PUBLIC": str(destination),
        "VONK_TEST_RECEIPT": str(receipt),
    }

    result = subprocess.run(
        ["sh", destination / "nas"],
        cwd=tmp_path,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert receipt.read_text().startswith("--template ")
    # The setup program names the exact rerun command from this URL.
    assert receipt.read_text().splitlines()[1] == "https://install.example.test/nas"
    receipt.unlink()
    manifest = destination / "artifacts/stable/current.manifest"
    manifest.write_bytes(
        manifest.read_bytes().replace(b"version=1.2.3", b"version=1.2.4")
    )
    rejected = subprocess.run(
        ["sh", destination / "nas"],
        cwd=tmp_path,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    assert rejected.returncode != 0
    assert "signature is invalid" in rejected.stderr
    assert not receipt.exists()


def test_public_cli_endpoint_verifies_the_signed_wheel_before_uv_install(
    tmp_path: Path,
) -> None:
    inputs = _inputs(tmp_path / "inputs")
    publication = _assemble(tmp_path / "inputs", inputs)
    destination = tmp_path / "public"
    published = _publish_accepted(publication, destination, tmp_path / "acceptance")
    assert published.returncode == 0, published.stderr
    commands = tmp_path / "commands"
    commands.mkdir()
    curl = commands / "curl"
    curl.write_text(
        "#!/bin/sh\nset -eu\ndestination=\nurl=\n"
        "while [ $# -gt 0 ]; do case $1 in -o) destination=$2; shift 2;; -*) shift;; *) url=$1; shift;; esac; done\n"
        'path=${url#https://install.example.test/}\ncp "$VONK_TEST_PUBLIC/$path" "$destination"\n'
    )
    curl.chmod(0o755)
    receipt = tmp_path / "uv-receipt"
    uv = commands / "uv"
    # `uv run … python -` runs the wheel's identity check on this interpreter.
    uv.write_text(
        '#!/bin/sh\nif [ "$1" = run ]; then\n'
        '  while [ "$1" != python ]; do shift; done; shift\n'
        '  exec "$VONK_TEST_PYTHON" "$@"\nfi\n'
        'printf \'%s\\n\' "$*" > "$VONK_TEST_UV_RECEIPT"\n'
    )
    uv.chmod(0o755)
    environment = {
        **os.environ,
        "PATH": f"{commands}:{os.environ['PATH']}",
        "VONK_INSTALL_BASE_URL": "https://install.example.test",
        "VONK_TEST_PUBLIC": str(destination),
        "VONK_TEST_PYTHON": sys.executable,
        "VONK_TEST_UV_RECEIPT": str(receipt),
        "HOME": str(tmp_path / "home"),
        "XDG_CONFIG_HOME": str(tmp_path / "home/.config"),
    }

    result = subprocess.run(
        ["sh", destination / "vonkctl"],
        cwd=tmp_path,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "vonkctl 1.2.3 is installed" in result.stdout
    assert "tool install --force" in receipt.read_text()
    # Dependencies resolve as of the release's stamped cutoff.
    assert f"--exclude-newer {DEPENDENCIES_EXCLUDE_NEWER} " in receipt.read_text()
    receipt.unlink()
    cli_artifact = next(destination.glob("artifacts/stable/releases/*/cli/*.whl"))
    cli_artifact.write_bytes(cli_artifact.read_bytes() + b"tampered")
    rejected = subprocess.run(
        ["sh", destination / "vonkctl"],
        cwd=tmp_path,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    assert rejected.returncode != 0
    assert (
        "CLI wheel size is invalid" in rejected.stderr
        or "CLI wheel digest is invalid" in rejected.stderr
    )
    assert not receipt.exists()


@pytest.mark.needs_installer_release_probe
def test_actual_publisher_manifest_is_complete_at_the_signed_rust_boundary(
    tmp_path: Path,
) -> None:
    probe = os.environ.get("VONK_INSTALLER_RELEASE_WIRE_PROBE")
    if not probe:
        pytest.skip("VONK_INSTALLER_RELEASE_WIRE_PROBE requires the native Rust probe")

    inputs = _inputs(tmp_path)
    publication = _assemble(tmp_path, inputs)
    plan = json.loads((publication / "publication-plan.json").read_bytes())
    root = publication / "objects"
    release_paths = [
        root / item["key"]
        for item in plan["objects"]
        if item["key"].endswith("/release.json")
    ]
    assert len(release_paths) == 2
    schema = json.loads(
        (ROOT / "schemas/install-release-manifest.schema.json").read_bytes()
    )
    validator = Draft202012Validator(schema)
    loaded_signing_key = serialization.load_pem_private_key(
        _input_path(inputs, "signing_key").read_bytes(), password=None
    )
    assert isinstance(loaded_signing_key, rsa.RSAPrivateKey)
    signing_key = loaded_signing_key

    def consume(path: Path, signature: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [probe, str(path), str(signature), str(inputs["signing_public_key"])],
            text=True,
            capture_output=True,
            check=False,
        )

    for release_path in release_paths:
        raw = release_path.read_bytes()
        document = json.loads(raw)
        validator.validate(document)
        model = InstallerReleaseManifest.model_validate_json(raw)
        assert model.model_dump(mode="json", by_alias=True) == document
        consumed = consume(release_path, release_path.with_suffix(".sig"))
        assert consumed.returncode == 0, consumed.stderr
        assert json.loads(consumed.stdout) == document

        mutations = []
        missing_image = copy.deepcopy(document)
        del missing_image["images"]["worker"]
        mutations.append(missing_image)
        missing_artifact = copy.deepcopy(document)
        del missing_artifact["artifacts"]["spark-setup-signature-linux-arm64"]
        mutations.append(missing_artifact)
        extra_artifact = copy.deepcopy(document)
        extra_artifact["artifacts"]["obsolete-setup"] = copy.deepcopy(
            next(iter(document["artifacts"].values()))
        )
        mutations.append(extra_artifact)
        extra_root = copy.deepcopy(document)
        extra_root["retired_schema"] = 1
        mutations.append(extra_root)
        if not document.get("acceptance_only"):
            missing_cli = copy.deepcopy(document)
            del missing_cli["artifacts"]["cli-wheel"]
            mutations.append(missing_cli)
            missing_package_identity = copy.deepcopy(document)
            del missing_package_identity["artifacts"]["agent-package-linux-arm64"][
                "target_binary_digest"
            ]
            mutations.append(missing_package_identity)
            missing_nas = copy.deepcopy(document)
            del missing_nas["artifacts"]["nas-setup-wheel"]
            mutations.append(missing_nas)
        else:
            missing_role = copy.deepcopy(document)
            del missing_role["acceptance_only"]
            mutations.append(missing_role)
        for malformed in mutations:
            with pytest.raises(ValidationError):
                InstallerReleaseManifest.model_validate(malformed)
            assert not validator.is_valid(malformed)
            changed = tmp_path / "malformed-release.json"
            _canonical(changed, malformed)
            signature = tmp_path / "malformed-release.sig"
            signature.write_bytes(
                base64.b64encode(
                    signing_key.sign(
                        changed.read_bytes(), padding.PKCS1v15(), hashes.SHA256()
                    )
                )
                + b"\n"
            )
            # Valid signatures must not allow incomplete or extra graph fields.
            assert consume(changed, signature).returncode != 0
        bad_signature = tmp_path / "corrupt.sig"
        bad_signature.write_bytes(base64.b64encode(b"x" * 256) + b"\n")
        assert consume(release_path, bad_signature).returncode != 0
