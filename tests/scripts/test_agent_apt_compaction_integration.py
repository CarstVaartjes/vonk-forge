from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/agent-apt-state"
SOURCE_SHA = "0123456789abcdef0123456789abcdef01234567"
REPOSITORIES = {"dev": "vonk-forge-dev", "stable": "vonk-forge"}


def load_state_module() -> ModuleType:
    loader = SourceFileLoader("agent_apt_state_integration", str(SCRIPT))
    spec = spec_from_loader(loader.name, loader)
    assert spec is not None
    module = module_from_spec(spec)
    sys.modules[loader.name] = module
    loader.exec_module(module)
    return module


def aptly(config: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["aptly", f"-config={config}", *arguments],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    return completed.stdout


def package(directory: Path, version: str, architecture: str) -> Path:
    source = directory / f"source-{version}-{architecture}"
    control = source / "DEBIAN/control"
    control.parent.mkdir(parents=True)
    control.write_text(
        "\n".join(
            (
                "Package: vonk-forge-agent",
                f"Version: {version}",
                f"Architecture: {architecture}",
                "Maintainer: Vonk Forge <packages@vonkforge.ai>",
                "Description: bounded aptly state integration fixture",
                "",
            )
        )
    )
    payload = source / "usr/share/vonk-forge-agent/version"
    payload.parent.mkdir(parents=True)
    payload.write_text(f"{version}-{architecture}\n")
    target = directory / f"vonk-forge-agent_{version}_{architecture}.deb"
    completed = subprocess.run(
        ["dpkg-deb", "--build", "--root-owner-group", source, target],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    return target


def receipt(channel: str, version: str, package_dir: Path) -> dict[str, object]:
    packages = {
        architecture: package(package_dir, version, architecture)
        for architecture in ("arm64",)
    }
    return {
        "channel": channel,
        "distribution": channel,
        "packages": {
            architecture: {
                "filename": path.name,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
            for architecture, path in packages.items()
        },
        "snapshot": f"{channel}-{version}",
        "source_sha": SOURCE_SHA,
        "version": version,
    }


def run_channel(
    tmp_path: Path, channel: str, versions: tuple[str, ...]
) -> tuple[list[int], bytes]:
    state = load_state_module()
    work = tmp_path / channel
    package_dir = work / "packages"
    package_dir.mkdir(parents=True)
    public = work / "public-0"
    public.mkdir()
    aptly_root = work / "aptly"
    config = work / "aptly.json"
    config.write_text(
        json.dumps(
            {
                "rootDir": str(aptly_root),
                "architectures": ["arm64"],
                "FileSystemPublishEndpoints": {
                    "integration": {
                        "rootDir": str(public),
                        "linkMethod": "copy",
                    }
                },
            }
        )
    )
    repository = REPOSITORIES[channel]
    aptly(
        config,
        "repo",
        "create",
        f"-distribution={channel}",
        "-component=main",
        repository,
    )
    state_sizes: list[int] = []
    first_public_package: bytes | None = None
    first_public_path: Path | None = None

    for position, version in enumerate(versions):
        if position:
            public = work / f"public-{position}"
            public.mkdir()
            configuration = json.loads(config.read_text())
            configuration["FileSystemPublishEndpoints"]["integration"][
                "rootDir"
            ] = str(public)
            config.write_text(json.dumps(configuration))
        publication = receipt(channel, version, package_dir)
        state.compact_aptly_state(
            publication,
            config,
            repository,
            package_dir,
            public,
            "prepare",
        )
        state.compact_aptly_state(
            publication,
            config,
            repository,
            package_dir,
            public,
            "prepare",
        )
        aptly(
            config,
            "snapshot",
            "create",
            publication["snapshot"],
            "from",
            "repo",
            repository,
        )
        if position == 0:
            aptly(
                config,
                "publish",
                "snapshot",
                "-skip-signing",
                "-architectures=arm64",
                f"-distribution={channel}",
                "-component=main",
                publication["snapshot"],
                "filesystem:integration:",
            )
        else:
            aptly(
                config,
                "publish",
                "switch",
                "-skip-signing",
                channel,
                "filesystem:integration:",
                publication["snapshot"],
            )
        state.compact_aptly_state(
            publication,
            config,
            repository,
            package_dir,
            public,
            "finalize",
        )
        state.compact_aptly_state(
            publication,
            config,
            repository,
            package_dir,
            public,
            "finalize",
        )
        replay_public = work / f"public-replay-{position}"
        replay_public.mkdir()
        configuration = json.loads(config.read_text())
        configuration["FileSystemPublishEndpoints"]["integration"][
            "rootDir"
        ] = str(replay_public)
        config.write_text(json.dumps(configuration))
        aptly(
            config,
            "publish",
            "switch",
            "-skip-signing",
            channel,
            "filesystem:integration:",
            publication["snapshot"],
        )
        state.compact_aptly_state(
            publication,
            config,
            repository,
            package_dir,
            replay_public,
            "finalize",
        )
        assert aptly(config, "snapshot", "list", "-raw").splitlines() == [
            publication["snapshot"]
        ]
        records = state._repository_records(config, repository)
        expected_versions = 1 if channel == "dev" else min(position + 1, 3)
        assert len(records) == expected_versions
        assert state._sorted_versions({record[1] for record in records})[-1] == version
        state_sizes.append(len(state.build_bundle(aptly_root, "state", publication)))
        if first_public_path is None:
            index = public / f"dists/{channel}/main/binary-arm64/Packages"
            fields = next(
                item
                for item in state._debian_control_paragraphs(index.read_bytes())
                if item["Version"] == version
            )
            first_public_path = public / fields["Filename"]
            first_public_package = first_public_path.read_bytes()
        else:
            assert first_public_path.read_bytes() == first_public_package

    warm_sizes = state_sizes[2:]
    assert max(warm_sizes) - min(warm_sizes) < 128 * 1024
    assert first_public_package is not None
    return state_sizes, first_public_package


@pytest.mark.skipif(
    shutil.which("aptly") is None or shutil.which("dpkg-deb") is None,
    reason="real aptly integration requires Linux aptly and dpkg-deb",
)
def test_real_aptly_compaction_is_bounded_and_preserves_public_pool(
    tmp_path: Path,
) -> None:
    dev_sizes, _ = run_channel(
        tmp_path,
        "dev",
        (
            "0.1.0~dev.100+g0123456789ab",
            "0.1.0~dev.101+g0123456789ab",
            "0.1.0~dev.102+g0123456789ab",
            "0.1.0~dev.103+g0123456789ab",
        ),
    )
    stable_sizes, _ = run_channel(
        tmp_path,
        "stable",
        ("1.8.0", "1.9.0", "1.10.0", "1.11.0", "1.12.0"),
    )
    assert max(dev_sizes) < 4 * 1024 * 1024
    assert max(stable_sizes) < 4 * 1024 * 1024
