from __future__ import annotations

import importlib.machinery
import importlib.util
import shutil
import subprocess
import uuid
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
SCANNER = ROOT / "scripts" / "verify-dev-image-secrets"


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


def _scanner_module():
    loader = importlib.machinery.SourceFileLoader("verify_dev_image_secrets", str(SCANNER))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


@pytest.fixture
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
                "-t",
                image,
                str(ROOT),
            ),
            check=True,
        )
    yield images
    for image in images.values():
        subprocess.run(("docker", "image", "rm", "--force", image), check=False, capture_output=True)


def test_scanner_accepts_clean_nonroot_images(image_factory) -> None:
    image = image_factory()

    result = _scan(image, image)

    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
    assert result.stderr == ""


def test_scanner_does_not_treat_embedded_binary_strings_as_private_key_material(
    image_factory,
) -> None:
    image = image_factory(
        files={
            "bin/tool": b"\0binary-prefix\0-----BEGIN PRIVATE KEY-----\n"
            b"c3ludGhldGljLXNlY3JldC1tYXRlcmlhbA==\n"
            b"-----END PRIVATE KEY-----\n"
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
    ("path", "content"),
    (
        (".dev/runtime", b"not a secret\n"),
        ("repository/.git/config", b"[core]\n"),
        ("settings/.env.local", b"not a secret\n"),
        ("keys/id_ed25519", b"not a secret\n"),
        ("config/credentials.json", b"{}\n"),
        (
            "notes.txt",
            b"-----BEGIN PRIVATE KEY-----\n"
            b"c3ludGhldGljLXNlY3JldC1tYXRlcmlhbA==\n"
            b"-----END PRIVATE KEY-----\n",
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
            "import shutil; assert not shutil.which('ssh') and not shutil.which('ssh-keygen')",
        ),
        check=False,
    )
    assert api_tools.returncode == 0
    assert worker_tools.returncode == 0


def test_scanner_command_is_executable() -> None:
    assert SCANNER.is_file()
    assert SCANNER.stat().st_mode & 0o111
