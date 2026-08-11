from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import subprocess
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCANNER = ROOT / "scripts" / "verify-dev-image-secrets"
PRIVATE_KEY_BLOCK = (
    b"-----BEGIN PRIVATE KEY-----\n"
    b"c3ludGhldGljLXNlY3JldC1tYXRlcmlhbA==\n"
    b"-----END PRIVATE KEY-----\n"
)
PRIVATE_KEY_TEXT = PRIVATE_KEY_BLOCK.decode()
SOURCE_COMMIT = "0123456789abcdef0123456789abcdef01234567"
OTHER_COMMIT = "fedcba9876543210fedcba9876543210fedcba98"
SOURCE_REPOSITORY = "https://github.com/CarstVaartjes/vonk-forge"
OTHER_REPOSITORY = "https://github.com/example/fork"
BUILD_DIGEST = "sha256:417d194fbdb2ae0359258796aed4b84f4a15466697774f633bf6c0ca94b10c5d"


def _embedded_identity_document(**overrides: object) -> dict[str, object]:
    document: dict[str, object] = {
        "build_digest": BUILD_DIGEST,
        "channel": "development",
        "database_revision": "0020_recipe_catalog_bridge",
        "image_role": "api",
        "platform_version": "0.1.0",
        "protocol_maximum": 3,
        "protocol_minimum": 1,
        "schema_version": 1,
        "source_commit": SOURCE_COMMIT,
        "source_repository": SOURCE_REPOSITORY,
    }
    document.update(overrides)
    return document


def _canonical_identity_output(**overrides: object) -> str:
    return (
        json.dumps(
            _embedded_identity_document(**overrides),
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    )


def _accepted_image_inspection() -> dict[str, object]:
    return {
        "Config": {
            "Labels": {
                "org.opencontainers.image.revision": SOURCE_COMMIT,
                "org.opencontainers.image.source": SOURCE_REPOSITORY,
            }
        }
    }


@pytest.fixture
def image_factory(tmp_path: Path):
    built: list[str] = []

    def build(*, files: dict[str, bytes] | None = None, dockerfile: str | None = None) -> str:
        context = tmp_path / uuid.uuid4().hex
        context.mkdir()
        for name, content in (files or {"healthy.txt": b"all clear\n"}).items():
            destination = context / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(content)
        (context / "Dockerfile").write_text(
            dockerfile
            or "FROM scratch\nCOPY . /scan/\nUSER 10001:10001\n",
            encoding="utf-8",
        )
        image = f"vonk-forge-secret-scan-test:{uuid.uuid4().hex}"
        subprocess.run(("docker", "build", "--quiet", "-t", image, str(context)), check=True)
        built.append(image)
        return image

    yield build

    for image in built:
        subprocess.run(("docker", "image", "rm", "--force", image), check=False, capture_output=True)


def _scan(*images: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        (str(SCANNER), *images),
        check=False,
        text=True,
        capture_output=True,
    )


def _canary_dir(tmp_path: Path, **contents: bytes) -> Path:
    root = tmp_path / "canaries"
    root.mkdir()
    for name, content in contents.items():
        (root / name).write_bytes(content)
    return root


def _accept(
    api_image: str,
    worker_image: str,
    *,
    commit: str = SOURCE_COMMIT,
    repository: str = SOURCE_REPOSITORY,
) -> subprocess.CompletedProcess[str]:
    return _scan(
        "--expected-commit",
        commit,
        "--expected-repository",
        repository,
        api_image,
        worker_image,
    )


def _scanner_module():
    loader = importlib.machinery.SourceFileLoader("verify_dev_image_secrets", str(SCANNER))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def _assert_embedded_identity_output_rejected(
    monkeypatch: pytest.MonkeyPatch, output: str
) -> None:
    scanner = _scanner_module()
    commands: list[tuple[str, ...]] = []

    def fake_run(arguments):
        command = tuple(arguments)
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, output, "")

    monkeypatch.setattr(scanner, "_run", fake_run)

    with pytest.raises(scanner.ScanFailure, match="identity verification failed"):
        scanner._verify_image_identity(
            "vonk-forge-api:accepted-labels",
            _accepted_image_inspection(),
            expected_role="api",
            expected_commit=SOURCE_COMMIT,
            expected_repository=SOURCE_REPOSITORY,
        )

    assert len(commands) == 1
    assert commands[0][:3] == ("docker", "run", "--rm")


@pytest.fixture(scope="module")
def local_development_images():
    suffix = uuid.uuid4().hex
    images = {
        target: f"vonk-forge-{target}:image-contract-{suffix}"
        for target in ("api", "worker")
    }
    for target, image in images.items():
        subprocess.run(
            (
                "docker",
                "build",
                "--quiet",
                "-f",
                str(ROOT / "control" / "Dockerfile"),
                "--target",
                target,
                "--build-arg",
                f"VONK_DEV_SOURCE_COMMIT={SOURCE_COMMIT}",
                "--label",
                f"org.opencontainers.image.source={SOURCE_REPOSITORY}",
                "--label",
                f"org.opencontainers.image.revision={SOURCE_COMMIT}",
                "-t",
                image,
                str(ROOT),
            ),
            check=True,
        )
    yield images
    for image in images.values():
        subprocess.run(("docker", "image", "rm", "--force", image), check=False, capture_output=True)


@pytest.fixture(scope="module")
def mislabeled_api_image():
    image = f"vonk-forge-api:image-contract-mislabeled-{uuid.uuid4().hex}"
    subprocess.run(
        (
            "docker",
            "build",
            "--quiet",
            "-f",
            str(ROOT / "control" / "Dockerfile"),
            "--target",
            "api",
            "--build-arg",
            f"VONK_DEV_SOURCE_COMMIT={SOURCE_COMMIT}",
            "--label",
            f"org.opencontainers.image.source={SOURCE_REPOSITORY}",
            "--label",
            f"org.opencontainers.image.revision={OTHER_COMMIT}",
            "-t",
            image,
            str(ROOT),
        ),
        check=True,
    )
    yield image
    subprocess.run(
        ("docker", "image", "rm", "--force", image),
        check=False,
        capture_output=True,
    )


def test_scanner_accepts_clean_nonroot_images(image_factory) -> None:
    image = image_factory()

    result = _scan(image, image)

    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
    assert result.stderr == ""


def test_scanner_accepts_path_only_mode_for_clean_publication_artifacts(
    tmp_path: Path,
) -> None:
    artifact_root = tmp_path / "artifact"
    artifact_root.mkdir()
    (artifact_root / "docker-compose.dev.yml").write_text("services: {}\n", encoding="utf-8")
    (artifact_root / "provenance.json").write_text('{"predicateType":"clean"}\n', encoding="utf-8")
    canaries = _canary_dir(tmp_path, opaque=b"secret-canary-opaque\n")

    result = _scan(
        "--forbid-bytes-dir",
        str(canaries),
        "--scan-path",
        str(artifact_root),
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
    assert result.stderr == ""


def test_path_scan_streams_complete_oci_sized_artifacts_past_image_file_limit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    scanner = _scanner_module()
    canary = b"archive-canary"
    archive = tmp_path / "image.oci.tar"
    archive.write_bytes(b"clean-prefix-beyond-limit-" + canary)
    monkeypatch.setattr(scanner, "MAX_FILE_BYTES", 8)

    with pytest.raises(scanner.ScanFailure, match="canary"):
        scanner._scan_path(archive, forbidden_bytes=(canary,))


def test_scanner_rejects_canary_bytes_in_generated_workflow_artifacts_without_leaks(
    image_factory,
    tmp_path: Path,
) -> None:
    image = image_factory()
    canary = b"secret-canary-opaque\n"
    canaries = _canary_dir(tmp_path, database_url=canary)
    artifact = tmp_path / "dist" / "docker-compose.dev.yml"
    artifact.parent.mkdir()
    artifact.write_bytes(b"header\n" + canary + b"footer\n")

    result = _scan(
        "--forbid-bytes-dir",
        str(canaries),
        "--scan-path",
        str(artifact),
        image,
        image,
    )

    assert result.returncode == 1
    assert canary.decode().strip() not in result.stdout
    assert canary.decode().strip() not in result.stderr


def test_scanner_rejects_private_key_markers_in_generated_publication_files_without_leaks(
    image_factory,
    tmp_path: Path,
) -> None:
    image = image_factory()
    provenance = tmp_path / "provenance.json"
    provenance.write_bytes(PRIVATE_KEY_BLOCK)

    result = _scan("--scan-path", str(provenance), image, image)

    assert result.returncode == 1
    assert "c3ludGhldGljLXNlY3JldC1tYXRlcmlhbA" not in result.stdout
    assert "c3ludGhldGljLXNlY3JldC1tYXRlcmlhbA" not in result.stderr


def test_scanner_rejects_secret_canary_bytes_in_image_metadata_values(
    image_factory,
    tmp_path: Path,
) -> None:
    image = image_factory(
        dockerfile=(
            "FROM scratch\n"
            "COPY . /scan/\n"
            "LABEL org.example.note=workflow-canary-opaque\n"
            "USER 10001:10001\n"
        )
    )
    canaries = _canary_dir(tmp_path, note=b"workflow-canary-opaque")

    result = _scan("--forbid-bytes-dir", str(canaries), image, image)

    assert result.returncode == 1
    assert "workflow-canary-opaque" not in result.stdout
    assert "workflow-canary-opaque" not in result.stderr


@pytest.mark.parametrize(
    "content",
    (
        b"\0" + PRIVATE_KEY_BLOCK,
        b"text-prefix\n" + PRIVATE_KEY_BLOCK + b"\0",
        b"x" * (64 * 1024 - 24) + b"\0" + PRIVATE_KEY_BLOCK,
    ),
    ids=("single-nul-prefix", "later-single-nul", "chunk-boundary-after-nul"),
)
def test_scanner_rejects_private_key_blocks_anywhere_in_regular_bytes(
    image_factory, content: bytes
) -> None:
    image = image_factory(files={"bin/tool": content})

    result = _scan(image, image)

    assert result.returncode == 1
    assert "c3ludGhldGljLXNlY3JldC1tYXRlcmlhbA" not in result.stdout
    assert "c3ludGhldGljLXNlY3JldC1tYXRlcmlhbA" not in result.stderr


def test_scanner_rejects_long_private_key_block_across_many_chunks(
    image_factory,
) -> None:
    content = (
        b"-----BEGIN PRIVATE KEY-----\n"
        + (b"A" * 64 + b"\n") * 3_100
        + b"-----END PRIVATE KEY-----\n"
    )
    image = image_factory(files={"long-key.txt": content})

    result = _scan(image, image)

    assert result.returncode == 1
    assert b"A" * 64 not in result.stdout.encode()
    assert b"A" * 64 not in result.stderr.encode()


def test_scanner_allows_private_key_fixture_only_in_dense_compiled_like_binary(
    image_factory,
) -> None:
    compiled_bytes = b"\x7fELF" + (b"\0\x01\x02\x03compiled-code" * 512)
    image = image_factory(
        files={"usr/lib/libfixture.so": compiled_bytes + PRIVATE_KEY_BLOCK + compiled_bytes}
    )

    result = _scan(image, image)

    assert result.returncode == 0, result.stderr


def test_scanner_accepts_private_key_regex_source_without_key_material(
    image_factory,
) -> None:
    image = image_factory(
        files={
            "scanner.py": (
                b"pattern = rb'-----BEGIN(?: [A-Z0-9]+)* PRIVATE KEY-----'\n"
                b"footer = rb'-----END(?: [A-Z0-9]+)* PRIVATE KEY-----'\n"
            )
        }
    )

    result = _scan(image, image)

    assert result.returncode == 0, result.stderr


def test_scanner_allows_only_documented_secret_file_paths(image_factory) -> None:
    image = image_factory(
        dockerfile=(
            "FROM scratch\nCOPY . /scan/\n"
            "ENV VONK_GIT_SIGNING_KEY_FILE=/run/secrets/git-signing-key\n"
            "USER 10001:10001\n"
        )
    )

    result = _scan(image, image)

    assert result.returncode == 0, result.stderr


def test_scanner_rejects_split_secret_command_arguments() -> None:
    scanner = _scanner_module()

    with pytest.raises(scanner.ScanFailure, match="command authority"):
        scanner._check_metadata(
            {
                "Config": {
                    "User": "10001:10001",
                    "Env": [],
                    "Labels": {},
                    "Entrypoint": ["--password", "do-not-print"],
                    "Cmd": None,
                }
            },
            [],
        )


@pytest.mark.parametrize(
    "name",
    ("VONK_APIKEY", "VONK_SSHKEY", "VONK_PRIVATEKEY", "VONK_SIGNINGKEY"),
)
def test_scanner_rejects_concatenated_environment_authority(name: str) -> None:
    scanner = _scanner_module()

    with pytest.raises(scanner.ScanFailure, match="environment authority"):
        scanner._check_metadata(
            {
                "Config": {
                    "User": "10001:10001",
                    "Env": [f"{name}=never-print-metadata"],
                    "Labels": {},
                    "Entrypoint": None,
                    "Cmd": None,
                }
            },
            [],
        )


@pytest.mark.parametrize(
    "key",
    ("org.example.apikey", "org.example.sshkey", "org.example.privatekey", "org.example.signingkey"),
)
def test_scanner_rejects_concatenated_label_authority(key: str) -> None:
    scanner = _scanner_module()

    with pytest.raises(scanner.ScanFailure, match="label authority"):
        scanner._check_metadata(
            {
                "Config": {
                    "User": "10001:10001",
                    "Env": [],
                    "Labels": {key: "never-print-metadata"},
                    "Entrypoint": None,
                    "Cmd": None,
                }
            },
            [],
        )


@pytest.mark.parametrize(
    "dockerfile",
    (
        "FROM scratch\nCOPY . /scan/\nENV VONK_APIKEY=never-print-metadata\nUSER 10001:10001\n",
        "FROM scratch\nCOPY . /scan/\nENV VONK_SSHKEY=never-print-metadata\nUSER 10001:10001\n",
        "FROM scratch\nCOPY . /scan/\nENV VONK_PRIVATEKEY=never-print-metadata\nUSER 10001:10001\n",
        "FROM scratch\nCOPY . /scan/\nENV VONK_SIGNINGKEY=never-print-metadata\nUSER 10001:10001\n",
        "FROM scratch\nCOPY . /scan/\nLABEL org.example.apikey=never-print-metadata\nUSER 10001:10001\n",
        "FROM scratch\nCOPY . /scan/\nLABEL org.example.sshkey=never-print-metadata\nUSER 10001:10001\n",
        "FROM scratch\nCOPY . /scan/\nLABEL org.example.privatekey=never-print-metadata\nUSER 10001:10001\n",
        "FROM scratch\nCOPY . /scan/\nLABEL org.example.signingkey=never-print-metadata\nUSER 10001:10001\n",
    ),
)
def test_scanner_sanitizes_concatenated_metadata_authority_diagnostics(
    image_factory, dockerfile: str
) -> None:
    image = image_factory(dockerfile=dockerfile)

    result = _scan(image, image)

    assert result.returncode == 1
    assert "never-print-metadata" not in result.stdout
    assert "never-print-metadata" not in result.stderr


@pytest.mark.parametrize(
    ("config", "history"),
    (
        (
            {
                "User": "10001:10001",
                "Env": [f"BUILD_NOTE={PRIVATE_KEY_TEXT}"],
                "Labels": {},
                "Entrypoint": None,
                "Cmd": None,
            },
            [],
        ),
        (
            {
                "User": "10001:10001",
                "Env": [],
                "Labels": {"org.example.note": PRIVATE_KEY_TEXT},
                "Entrypoint": None,
                "Cmd": None,
            },
            [],
        ),
        (
            {
                "User": "10001:10001",
                "Env": [],
                "Labels": {},
                "Entrypoint": None,
                "Cmd": [PRIVATE_KEY_TEXT],
            },
            [],
        ),
        (
            {
                "User": "10001:10001",
                "Env": [],
                "Labels": {},
                "Entrypoint": None,
                "Cmd": None,
            },
            [{"CreatedBy": PRIVATE_KEY_TEXT}],
        ),
    ),
)
def test_scanner_rejects_private_key_material_in_metadata_values(
    config: dict[str, object], history: list[dict[str, str]]
) -> None:
    scanner = _scanner_module()

    with pytest.raises(scanner.ScanFailure):
        scanner._check_metadata({"Config": config}, history)


@pytest.mark.parametrize(
    ("config", "history"),
    (
        (
            {
                "User": "10001:10001",
                "Env": ["BUILD_NOTE=AWSACCESSKEYID=never-print-metadata"],
                "Labels": {},
                "Entrypoint": None,
                "Cmd": None,
            },
            [],
        ),
        (
            {
                "User": "10001:10001",
                "Env": [],
                "Labels": {"org.example.note": "clientSecret: never-print-metadata"},
                "Entrypoint": None,
                "Cmd": None,
            },
            [],
        ),
        (
            {
                "User": "10001:10001",
                "Env": [],
                "Labels": {},
                "Entrypoint": None,
                "Cmd": ["AWSACCESSKEYID=never-print-metadata"],
            },
            [],
        ),
        (
            {
                "User": "10001:10001",
                "Env": [],
                "Labels": {},
                "Entrypoint": None,
                "Cmd": None,
            },
            [{"CreatedBy": "ARG AWSACCESSKEYID=never-print-metadata"}],
        ),
        (
            {
                "User": "10001:10001",
                "Env": [],
                "Labels": {},
                "Entrypoint": None,
                "Cmd": None,
            },
            [
                {
                    "CreatedBy": (
                        "ARG AWSACCESSKEYID=never-print-metadata "
                        "ENV VONK_GIT_SIGNING_KEY_FILE=/run/secrets/git-signing-key"
                    )
                }
            ],
        ),
    ),
)
def test_scanner_rejects_embedded_credential_assignments_in_metadata_values(
    config: dict[str, object], history: list[dict[str, str]]
) -> None:
    scanner = _scanner_module()

    with pytest.raises(scanner.ScanFailure):
        scanner._check_metadata({"Config": config}, history)


@pytest.mark.parametrize(
    ("path", "content"),
    (
        (".dev/runtime", b"not a secret\n"),
        ("repository/.git/config", b"[core]\n"),
        ("settings/.env.local", b"not a secret\n"),
        ("keys/id_ed25519", b"not a secret\n"),
        ("config/credentials.json", b"{}\n"),
        (
            "notes.txt",
            PRIVATE_KEY_BLOCK,
        ),
    ),
)
def test_scanner_rejects_forbidden_files_without_disclosing_contents(
    image_factory, path: str, content: bytes
) -> None:
    image = image_factory(files={path: content})

    result = _scan(image, image)

    assert result.returncode == 1
    assert content.decode("utf-8", errors="ignore") not in result.stdout
    assert content.decode("utf-8", errors="ignore") not in result.stderr
    assert path not in result.stderr or path.rsplit("/", 1)[-1] in result.stderr


@pytest.mark.parametrize(
    "path",
    (
        "runtime/postgres-password",
        "runtime/database-url",
        "runtime/git-signing-key",
        "runtime/git-signing-key.pub",
        "runtime/worker-api-token",
        "runtime/admin-grant-private-key",
        "keys/id_ecdsa",
        "keys/id_dsa",
        "home/control/.npmrc",
        "home/control/.netrc",
        "home/control/.pypirc",
        "config/credentials.yaml",
        "config/credentials.yml",
        "config/credentials.toml",
        "config/secrets.json",
        "config/secrets.yaml",
        "config/secrets.yml",
        "config/secrets.toml",
        "home/control/.ssh/config",
        "home/control/.aws/config",
        "home/control/.kube/config",
        "home/control/.docker/config.json",
        "home/control/.azure/accessTokens.json",
        "home/control/.config/gcloud/application_default_credentials.json",
    ),
)
def test_scanner_rejects_every_forbidden_filesystem_name_family(
    image_factory, path: str
) -> None:
    marker = f"never-print-{path.replace('/', '-')}-value\n".encode()
    image = image_factory(files={path: marker})

    result = _scan(image, image)

    assert result.returncode == 1
    assert marker.decode().strip() not in result.stdout
    assert marker.decode().strip() not in result.stderr


@pytest.mark.parametrize(
    "dockerfile",
    (
        "FROM scratch\nCOPY . /scan/\nENV APP_SECRET=do-not-print\nUSER 10001:10001\n",
        "FROM alpine:3.22\nARG BUILD_SECRET=do-not-print\nRUN echo $BUILD_SECRET >/dev/null\nCOPY . /scan/\nUSER 10001:10001\n",
        "FROM scratch\nCOPY . /scan/\nLABEL org.example.token=do-not-print\nUSER 10001:10001\n",
        "FROM scratch\nCOPY . /scan/\nENTRYPOINT [\"--password=do-not-print\"]\nUSER 10001:10001\n",
        "FROM scratch\nCOPY . /scan/\nENTRYPOINT [\"--password\", \"do-not-print\"]\nUSER 10001:10001\n",
    ),
)
def test_scanner_rejects_secret_authority_metadata_without_value_leaks(
    image_factory, dockerfile: str
) -> None:
    image = image_factory(dockerfile=dockerfile)

    result = _scan(image, image)

    assert result.returncode == 1
    assert "do-not-print" not in result.stdout
    assert "do-not-print" not in result.stderr


def test_scanner_rejects_root_or_implicit_user(image_factory) -> None:
    image = image_factory(dockerfile="FROM scratch\nCOPY . /scan/\n")

    result = _scan(image, image)

    assert result.returncode == 1
    assert "secret-boundary" in result.stderr


def test_scanner_removes_its_temporary_containers(image_factory) -> None:
    image = image_factory()

    result = _scan(image, image)

    assert result.returncode == 0, result.stderr
    containers = subprocess.run(
        ("docker", "ps", "--all", "--filter", "label=vonk-forge.secret-scan=true", "--format", "{{.ID}}"),
        check=True,
        text=True,
        capture_output=True,
    ).stdout.splitlines()
    assert containers == []


def test_scanner_removes_temporary_containers_after_filesystem_failure(image_factory) -> None:
    image = image_factory(files={".dev/runtime": b"forbidden\n"})

    result = _scan(image, image)

    assert result.returncode == 1
    containers = subprocess.run(
        ("docker", "ps", "--all", "--filter", "label=vonk-forge.secret-scan=true", "--format", "{{.ID}}"),
        check=True,
        text=True,
        capture_output=True,
    ).stdout.splitlines()
    assert containers == []


@pytest.mark.parametrize(
    ("removal_returncode", "remaining"),
    ((1, ""), (0, "scanner-container\n")),
)
def test_scanner_fails_closed_when_its_exact_container_is_not_cleaned_up(
    monkeypatch: pytest.MonkeyPatch, removal_returncode: int, remaining: str
) -> None:
    scanner = _scanner_module()
    commands: list[tuple[str, ...]] = []

    monkeypatch.setattr(scanner, "_inspect", lambda _image: {})
    monkeypatch.setattr(scanner, "_history", lambda _image: [])
    monkeypatch.setattr(scanner, "_check_metadata", lambda _image, _history: None)
    monkeypatch.setattr(scanner, "_scan_export", lambda _container: None)

    def fake_run(arguments):
        command = tuple(arguments)
        commands.append(command)
        if command[:2] == ("docker", "create"):
            return subprocess.CompletedProcess(command, 0, "scanner-container\n", "")
        if command[:3] == ("docker", "rm", "--force"):
            if removal_returncode:
                raise subprocess.CalledProcessError(removal_returncode, command)
            return subprocess.CompletedProcess(command, 0, "scanner-container\n", "")
        if command[:3] == ("docker", "ps", "--all"):
            return subprocess.CompletedProcess(command, 0, remaining, "")
        raise AssertionError(f"unexpected command: {command}")

    def fake_subprocess_run(arguments, **_kwargs):
        command = tuple(arguments)
        commands.append(command)
        return subprocess.CompletedProcess(command, removal_returncode, "", "")

    monkeypatch.setattr(scanner, "_run", fake_run)
    monkeypatch.setattr(scanner.subprocess, "run", fake_subprocess_run)

    with pytest.raises(scanner.ScanFailure, match="cleanup"):
        scanner.scan_image("synthetic:latest")

    removal_commands = [command for command in commands if command[:2] == ("docker", "rm")]
    assert removal_commands == [("docker", "rm", "--force", "scanner-container")]


def test_development_targets_are_scannable_nonroot_and_have_only_required_git_tools(
    local_development_images,
) -> None:
    images = local_development_images

    for image in images.values():
        platform = subprocess.run(
            ("docker", "image", "inspect", "--format", "{{.Os}}/{{.Architecture}}", image),
            check=True,
            text=True,
            capture_output=True,
        ).stdout.strip()
        assert platform == "linux/amd64"

    scan = _scan(images["api"], images["worker"])
    assert scan.returncode == 0, scan.stderr

    check = (
        "import os, shutil; "
        "from pathlib import Path; "
        "assert (os.getuid(), os.getgid()) == (10001, 10001); "
        "assert Path('/srv/vonk-control/alembic.ini').is_file(); "
        "assert Path('/srv/vonk-control/migrations/env.py').read_bytes(); "
        "assert shutil.which('git')"
    )
    for image in images.values():
        subprocess.run(("docker", "run", "--rm", image, "python", "-c", check), check=True)

    api_tools = subprocess.run(
        (
            "docker",
            "run",
            "--rm",
            images["api"],
            "python",
            "-c",
            "import shutil; assert shutil.which('ssh-keygen')",
        ),
        check=False,
    )
    worker_tools = subprocess.run(
        (
            "docker",
            "run",
            "--rm",
            images["worker"],
            "python",
            "-c",
            (
                "import shutil; from pathlib import Path; "
                "assert not shutil.which('ssh') and not shutil.which('ssh-keygen'); "
                "assert not shutil.which('docker'); "
                "assert not Path('/var/run/docker.sock').exists()"
            ),
        ),
        check=False,
    )
    assert api_tools.returncode == 0
    assert worker_tools.returncode == 0


def test_development_targets_embed_canonical_read_only_role_identity(
    local_development_images,
) -> None:
    check = (
        "import json, stat; "
        "from vonk_control.dev_cohort import DEVELOPMENT_IMAGE_IDENTITY_PATH, read_identity; "
        "identity=read_identity(DEVELOPMENT_IMAGE_IDENTITY_PATH, expected_role=__import__('sys').argv[1]); "
        "assert identity.source_commit == __import__('sys').argv[2]; "
        "assert identity.source_repository == __import__('sys').argv[3]; "
        "assert stat.S_IMODE(DEVELOPMENT_IMAGE_IDENTITY_PATH.stat().st_mode) == 0o444; "
        "print(json.dumps(identity.to_document(), sort_keys=True))"
    )

    for role, image in local_development_images.items():
        result = subprocess.run(
            (
                "docker",
                "run",
                "--rm",
                "--network=none",
                "--read-only",
                image,
                "python",
                "-c",
                check,
                role,
                SOURCE_COMMIT,
                SOURCE_REPOSITORY,
            ),
            check=True,
            text=True,
            capture_output=True,
        )
        assert json.loads(result.stdout)["image_role"] == role


def test_scanner_acceptance_verifies_both_embedded_identities(
    local_development_images,
) -> None:
    result = _accept(
        local_development_images["api"], local_development_images["worker"]
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
    assert result.stderr == ""


def test_scanner_acceptance_rejects_swapped_embedded_roles(
    local_development_images,
) -> None:
    result = _accept(
        local_development_images["worker"],
        local_development_images["api"],
    )

    assert result.returncode == 1
    assert "identity verification failed" in result.stderr


def test_scanner_acceptance_rejects_oci_revision_authority_mismatch(
    local_development_images,
    mislabeled_api_image: str,
) -> None:
    result = _accept(mislabeled_api_image, local_development_images["worker"])

    assert result.returncode == 1
    assert "identity verification failed" in result.stderr


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("source_commit", OTHER_COMMIT),
        ("source_repository", OTHER_REPOSITORY),
    ),
    ids=("commit", "repository"),
)
def test_scanner_rejects_embedded_identity_mismatch_after_authority_labels_match(
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: str,
) -> None:
    _assert_embedded_identity_output_rejected(
        monkeypatch,
        _canonical_identity_output(**{field: value}),
    )


def test_scanner_rejects_noncanonical_embedded_identity_bytes_after_labels_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    noncanonical = (
        json.dumps(_embedded_identity_document(), indent=2, sort_keys=True) + "\n"
    )

    _assert_embedded_identity_output_rejected(monkeypatch, noncanonical)


def test_scanner_command_is_executable() -> None:
    assert SCANNER.is_file()
    assert SCANNER.stat().st_mode & 0o111
