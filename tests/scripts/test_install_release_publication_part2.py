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

import install_release_publication_support as _support
import pytest
from install_release_publication_support import (
    DIGEST,
    ROOT,
    SCRIPT,
    SOURCE_SHA,
    _acceptance_receipt,
    _agent_package,
    _assemble,
    _assemble_command,
    _canonical,
    _input_mapping,
    _inputs,
    _promote,
    _publish_candidate,
)

publication = _support.publication

pytestmark = pytest.mark.needs_dpkg_deb


def _input_path(inputs: dict[str, object], key: str) -> Path:
    value = inputs[key]
    if not isinstance(value, Path):
        raise TypeError(f"release input {key!r} is not a path")
    return value


def _publish_accepted(
    publication: Path, destination: Path, acceptance_root: Path
) -> subprocess.CompletedProcess[str]:
    candidate = _publish_candidate(publication, destination)
    assert candidate.returncode == 0, candidate.stderr
    receipt, signature, public_key = _acceptance_receipt(acceptance_root, publication)
    return _promote(publication, destination, receipt, signature, public_key)


@pytest.mark.parametrize("preflight_only", [False, True])
def test_promotion_refuses_rejected_acceptance_evidence(
    tmp_path: Path,
    preflight_only: bool,
    publication: Path,
) -> None:
    destination = tmp_path / "public"
    assert _publish_candidate(publication, destination).returncode == 0
    receipt, signature, public_key = _acceptance_receipt(
        tmp_path / "acceptance", publication, status="rejected"
    )

    result = _promote(
        publication,
        destination,
        receipt,
        signature,
        public_key,
        preflight_only=preflight_only,
    )

    assert result.returncode == 2
    assert "evidence is not accepted" in result.stderr
    assert not (destination / "artifacts/stable/current.manifest").exists()


def test_assemble_refuses_an_expired_channel_manifest(tmp_path: Path) -> None:
    inputs = _inputs(tmp_path)
    command = _assemble_command(tmp_path, inputs)
    command[command.index("--expires-at") + 1] = "1"

    result = subprocess.run(
        command, cwd=ROOT, text=True, capture_output=True, check=False
    )

    assert result.returncode == 2
    assert "expiry is not in the future" in result.stderr


def test_assemble_rejects_package_metadata_from_another_release(tmp_path: Path) -> None:
    inputs = _inputs(tmp_path)
    wrong = _agent_package(tmp_path / "wrong", "linux-arm64", "1.2.2")
    renamed = tmp_path / "vonk-forge-agent_1.2.3_arm64.deb"
    renamed.write_bytes(wrong.read_bytes())
    _input_mapping(inputs, "packages")["linux-arm64"] = renamed

    result = subprocess.run(
        _assemble_command(tmp_path, inputs),
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 2
    assert "package metadata is inconsistent" in result.stderr


@pytest.mark.parametrize("role", ("api", "worker", "litellm"))
def test_assemble_refuses_mutable_image_references(tmp_path: Path, role: str) -> None:
    inputs = _inputs(tmp_path)
    command = _assemble_command(tmp_path, inputs)
    option = f"--{role}-image"
    command[command.index(option) + 1] = (
        f"ghcr.io/carstvaartjes/vonk-forge-{role}:latest@sha256:{DIGEST}"
    )

    result = subprocess.run(
        command, cwd=ROOT, text=True, capture_output=True, check=False
    )

    assert result.returncode == 2
    assert "image reference is mutable" in result.stderr


# Assembles a full release graph; CI runners run it 3-4x slower than local.
@pytest.mark.slow(60)
def test_development_assembly_reuses_images_from_an_accepted_ancestor(
    tmp_path: Path,
) -> None:
    inputs = _inputs(tmp_path, channel="dev")
    command = _assemble_command(tmp_path, inputs)
    images_source_sha = "c" * 40
    command[command.index("--images-source-sha") + 1] = images_source_sha
    for role in ("api", "worker", "litellm"):
        option = f"--{role}-image"
        command[command.index(option) + 1] = command[command.index(option) + 1].replace(
            f"dev-sha-{SOURCE_SHA}", f"dev-sha-{images_source_sha}"
        )

    result = subprocess.run(
        command, cwd=ROOT, text=True, capture_output=True, check=False
    )

    assert result.returncode == 0, result.stderr


# Assembles and signs a whole release publication through the real script.
@pytest.mark.slow(60)
@pytest.mark.parametrize(
    "image_tag", (f"acceptance-{SOURCE_SHA}-123-1", "dev-sha-" + "c" * 40)
)
def test_development_assembly_preserves_digest_pins_independent_of_tag_provenance(
    tmp_path: Path, image_tag: str
) -> None:
    command = _assemble_command(tmp_path, _inputs(tmp_path, channel="dev"))
    references: dict[str, str] = {}
    for role in ("api", "worker", "litellm"):
        index = command.index(f"--{role}-image") + 1
        reference = (
            f"ghcr.io/carstvaartjes/vonk-forge-{role}:{image_tag}@sha256:{DIGEST}"
        )
        command[index] = reference
        references[role] = reference

    result = subprocess.run(
        command, cwd=ROOT, text=True, capture_output=True, check=False
    )

    assert result.returncode == 0, result.stderr
    output = Path(command[command.index("--output") + 1])
    release_path = next(
        (output / "objects/artifacts/dev/releases").glob("*/release.json")
    )
    release = json.loads(release_path.read_text())
    for role, reference in references.items():
        assert release["images"][role] == reference


def test_promotion_writes_signed_atomic_manifest_after_acceptance_and_static_endpoints(
    tmp_path: Path,
) -> None:
    publication = _assemble(tmp_path, _inputs(tmp_path))
    destination = tmp_path / "public"

    result = _publish_accepted(publication, destination, tmp_path / "acceptance")

    assert result.returncode == 0, result.stderr
    operations = [json.loads(line) for line in result.stdout.splitlines()]
    assert operations[-1] == {
        "key": "artifacts/stable/current.manifest",
        "phase": "pointer",
    }
    assert all(item["phase"] == "acceptance" for item in operations[:-4])
    assert [item["phase"] for item in operations[-4:]] == [
        "endpoint",
        "endpoint",
        "endpoint",
        "pointer",
    ]
    pointer = destination / "artifacts/stable/current.manifest"
    lines = pointer.read_text().splitlines()
    assert lines[0] == "schema_version=2"
    assert lines[1] == "channel=stable"
    assert lines[-1].startswith("signature=")
    claims = tmp_path / "claims"
    signature = tmp_path / "signature"
    claims.write_text("\n".join(lines[:-1]) + "\n")
    signature.write_bytes(base64.b64decode(lines[-1].removeprefix("signature=")))
    verified = subprocess.run(
        [
            "openssl",
            "dgst",
            "-sha256",
            "-verify",
            publication / "channel-public-key.pem",
            "-signature",
            signature,
            claims,
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    assert verified.returncode == 0, verified.stderr
    assert (destination / "nas").is_file()
    assert (destination / "spark").is_file()


def test_static_endpoints_do_not_change_between_release_generations(
    tmp_path: Path,
) -> None:
    first_inputs = _inputs(tmp_path / "first")
    first = _assemble(tmp_path / "first", first_inputs)
    second_root = tmp_path / "second"
    second_inputs = _inputs(second_root)
    second_inputs["signing_key"] = first_inputs["signing_key"]
    second_inputs["signing_public_key"] = first_inputs["signing_public_key"]
    _canonical(
        _input_path(second_inputs, "payload"),
        {
            "docker_compose_yaml": "services:\n  control-api:\n    image: replacement\n",
            "schema_version": 2,
        },
    )
    command = _assemble_command(second_root, second_inputs)
    result = subprocess.run(
        command, cwd=ROOT, text=True, capture_output=True, check=False
    )
    assert result.returncode == 0, result.stderr
    second = second_root / "publication"
    assert (first / "objects/nas").read_bytes() == (second / "objects/nas").read_bytes()
    assert (first / "objects/spark").read_bytes() == (
        second / "objects/spark"
    ).read_bytes()
    assert (first / "objects/vonkctl").read_bytes() == (
        second / "objects/vonkctl"
    ).read_bytes()


def test_dev_promotion_serves_unqualified_endpoints_until_stable_exists(
    tmp_path: Path,
) -> None:
    publication = _assemble(tmp_path / "dev", _inputs(tmp_path / "dev", channel="dev"))
    destination = tmp_path / "public"

    result = _publish_accepted(publication, destination, tmp_path / "dev-acceptance")

    assert result.returncode == 0, result.stderr
    for name in ("nas", "spark", "vonkctl"):
        assert (destination / name).read_bytes() == (
            destination / "dev" / name
        ).read_bytes()
    assert b"channel='dev'" in (destination / "nas").read_bytes()
    operations = [json.loads(line) for line in result.stdout.splitlines()]
    assert [item["key"] for item in operations if item["phase"] == "endpoint"][3:] == [
        "nas",
        "spark",
        "vonkctl",
    ]


def _publication_pair(
    tmp_path: Path, *, second_channel: str = "stable", first_version: str = "1.2.3"
) -> tuple[Path, Path]:
    first_inputs = _inputs(tmp_path / "first", version=first_version)
    first = _assemble(tmp_path / "first", first_inputs)
    second_inputs = _inputs(tmp_path / "second", channel=second_channel)
    second_inputs["signing_key"] = first_inputs["signing_key"]
    second_inputs["signing_public_key"] = first_inputs["signing_public_key"]
    # Reuse the exact baseline archive across publications.
    second_inputs["baseline_packages"] = first_inputs["baseline_packages"]
    return first, _assemble(tmp_path / "second", second_inputs)


@pytest.fixture(scope="module")
def channel_publications(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    """Assemble once; each test still publishes to its own destination."""
    return _publication_pair(tmp_path_factory.mktemp("channels"), second_channel="dev")


@pytest.fixture(scope="module")
def version_publications(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    return _publication_pair(tmp_path_factory.mktemp("versions"), first_version="1.2.4")


def test_dev_promotion_never_replaces_unqualified_endpoints_once_stable_exists(
    tmp_path: Path,
    channel_publications: tuple[Path, Path],
) -> None:
    destination = tmp_path / "public"
    stable, dev = channel_publications
    first = _publish_accepted(stable, destination, tmp_path / "stable-acceptance")
    assert first.returncode == 0, first.stderr
    stable_nas = (destination / "nas").read_bytes()
    assert b"channel='stable'" in stable_nas

    second = _publish_accepted(dev, destination, tmp_path / "dev-acceptance")

    assert second.returncode == 0, second.stderr
    assert (destination / "nas").read_bytes() == stable_nas
    assert b"channel='dev'" in (destination / "dev/nas").read_bytes()


def test_stable_promotion_replaces_the_development_fallback_endpoints(
    tmp_path: Path,
    channel_publications: tuple[Path, Path],
) -> None:
    destination = tmp_path / "public"
    stable, dev = channel_publications
    published = _publish_accepted(dev, destination, tmp_path / "dev-acceptance")
    assert published.returncode == 0, published.stderr
    assert b"channel='dev'" in (destination / "nas").read_bytes()

    result = _publish_accepted(stable, destination, tmp_path / "stable-acceptance")

    assert result.returncode == 0, result.stderr
    assert b"channel='stable'" in (destination / "nas").read_bytes()


@pytest.mark.parametrize("preflight_only", [False, True])
def test_stable_publication_refuses_version_rollback_before_writing(
    tmp_path: Path,
    preflight_only: bool,
    version_publications: tuple[Path, Path],
) -> None:
    newest, older = version_publications
    destination = tmp_path / "public"
    first = _publish_accepted(newest, destination, tmp_path / "newest-acceptance")
    assert first.returncode == 0, first.stderr

    candidate = _publish_candidate(older, destination)
    assert candidate.returncode == 0, candidate.stderr
    receipt, signature, public_key = _acceptance_receipt(
        tmp_path / "older-acceptance", older
    )
    before = (destination / "artifacts/stable/current.manifest").read_bytes()
    rollback = _promote(
        older,
        destination,
        receipt,
        signature,
        public_key,
        preflight_only=preflight_only,
    )

    assert rollback.returncode == 2
    assert "refusing to regress" in rollback.stderr
    assert (destination / "artifacts/stable/current.manifest").read_bytes() == before


def test_refresh_extends_signed_manifest_after_verifying_all_release_objects(
    tmp_path: Path,
) -> None:
    inputs = _inputs(tmp_path / "inputs")
    publication = _assemble(tmp_path / "inputs", inputs)
    destination = tmp_path / "public"
    published = _publish_accepted(publication, destination, tmp_path / "acceptance")
    assert published.returncode == 0, published.stderr
    manifest = destination / "artifacts/stable/current.manifest"
    before = manifest.read_text().splitlines()
    expires_at = int(time.time()) + 14 * 24 * 60 * 60

    refreshed = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "refresh",
            "--channel",
            "stable",
            "--expires-at",
            str(expires_at),
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

    assert refreshed.returncode == 0, refreshed.stderr
    after = manifest.read_text().splitlines()
    assert after[:5] == before[:5]
    assert after[5] == f"expires_at={expires_at}"
    assert after[6:14] == before[6:14]
    assert after[14] != before[14]
    assert json.loads(refreshed.stdout) == {
        "channel": "stable",
        "phase": "pointer",
        "refreshed": True,
    }


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
    uv.write_text('#!/bin/sh\nprintf \'%s\\n\' "$*" > "$VONK_TEST_UV_RECEIPT"\n')
    uv.chmod(0o755)
    environment = {
        **os.environ,
        "PATH": f"{commands}:{os.environ['PATH']}",
        "VONK_INSTALL_BASE_URL": "https://install.example.test",
        "VONK_TEST_PUBLIC": str(destination),
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
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding, rsa
    from jsonschema import Draft202012Validator
    from pydantic import ValidationError
    from vonk_agent_protocol.installer_release import InstallerReleaseManifest

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
