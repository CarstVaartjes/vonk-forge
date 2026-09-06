from __future__ import annotations

import hashlib
import io
import re
import sys
import tarfile
import threading
from collections.abc import Callable
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/agent-apt-state"
SHA = "0123456789abcdef0123456789abcdef01234567"
PACKAGE_BYTES = {"arm64": b"arm64 package bytes"}


def load_state_module() -> ModuleType:
    assert SCRIPT.is_file(), "agent-apt-state has not been implemented"
    loaded = sys.modules.get("agent_apt_state")
    if loaded is not None:
        return loaded
    loader = SourceFileLoader("agent_apt_state", str(SCRIPT))
    spec = spec_from_loader(loader.name, loader)
    assert spec is not None
    module = module_from_spec(spec)
    sys.modules[loader.name] = module
    loader.exec_module(module)
    return module


def test_rclone_failure_reports_sanitized_provider_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = load_state_module()
    secrets = {
        "RCLONE_CONFIG_R2_ACCESS_KEY_ID": "example-access-key",
        "RCLONE_CONFIG_R2_SECRET_ACCESS_KEY": "example-secret-key",
        "RCLONE_CONFIG_R2_ENDPOINT": (
            "https://example-account.r2.cloudflarestorage.com"
        ),
    }
    for name, value in secrets.items():
        monkeypatch.setenv(name, value)
    stderr = (
        "request failed for https://url-user:url-password@example.invalid/object"
        "?signature=query-secret: AccessDenied "
        + " ".join(secrets.values())
    ).encode()
    monkeypatch.setattr(
        state.subprocess,
        "run",
        lambda *args, **kwargs: state.subprocess.CompletedProcess(
            args[0], 3, stdout=b"", stderr=stderr
        ),
    )

    with pytest.raises(state.StateError) as raised:
        state.RcloneStore("valid-bucket")._run(
            "write", ["copyto", "source", "target"]
        )

    message = str(raised.value)
    assert "write failed with exit code 3" in message
    assert "AccessDenied" in message
    for value in (*secrets.values(), "url-user", "url-password", "query-secret"):
        assert value not in message


def test_rclone_failure_without_stderr_reports_operation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = load_state_module()
    monkeypatch.setattr(
        state.subprocess,
        "run",
        lambda *args, **kwargs: state.subprocess.CompletedProcess(
            args[0], 9, stdout=b"", stderr=b""
        ),
    )

    with pytest.raises(state.StateError, match="read failed with exit code 9$"):
        state.RcloneStore("valid-bucket")._run("read", ["cat", "target"])


class FakeR2:
    def __init__(
        self,
        name: str,
        operations: list[str],
        *,
        fail_once: set[str] | None = None,
        corrupt_once: set[str] | None = None,
    ) -> None:
        self.name = name
        self.operations = operations
        self.objects: dict[str, bytes] = {}
        self.fail_once = set() if fail_once is None else set(fail_once)
        self.corrupt_once = set() if corrupt_once is None else set(corrupt_once)

    def list(self, prefix: str) -> list[str]:
        self.operations.append(f"{self.name}:list:{prefix}")
        return sorted(key for key in self.objects if key.startswith(prefix))

    def read(self, key: str) -> bytes:
        self.operations.append(f"{self.name}:read:{key}")
        return self.objects[key]

    def write(self, key: str, data: bytes) -> None:
        if key in self.fail_once:
            self.fail_once.remove(key)
            self.operations.append(f"{self.name}:write-failed:{key}")
            raise OSError("injected R2 failure")
        self.operations.append(f"{self.name}:write:{key}")
        if key in self.corrupt_once:
            self.corrupt_once.remove(key)
            self.objects[key] = b"corrupted in transit"
        else:
            self.objects[key] = data


class ConcurrentBundleR2(FakeR2):
    def __init__(self, name: str, operations: list[str]) -> None:
        super().__init__(name, operations)
        self.bundle_barrier = threading.Barrier(2)

    def write(self, key: str, data: bytes) -> None:
        if key.endswith(("/aptly-state.tar.gz", "/public-tree.tar.gz")):
            self.bundle_barrier.wait(timeout=2)
        super().write(key, data)


class ConcurrentReadR2(FakeR2):
    def __init__(
        self, name: str, operations: list[str], *, concurrent_read_count: int = 2
    ) -> None:
        super().__init__(name, operations)
        self.concurrent_reads: set[str] = set()
        self.read_barrier = threading.Barrier(concurrent_read_count)

    def read(self, key: str) -> bytes:
        if key in self.concurrent_reads:
            self.read_barrier.wait(timeout=2)
        return super().read(key)


class ConcurrentPublicR2(FakeR2):
    def __init__(self, name: str, operations: list[str]) -> None:
        super().__init__(name, operations)
        self.ordinary_barrier = threading.Barrier(2)
        self.release_barrier = threading.Barrier(2)

    def write(self, key: str, data: bytes) -> None:
        if key in {
            "dists/dev/main/binary-arm64/Packages",
            "vonk-forge-dev-archive-keyring.gpg",
        }:
            self.ordinary_barrier.wait(timeout=2)
        if key in {"dists/dev/Release", "dists/dev/Release.gpg"}:
            self.release_barrier.wait(timeout=2)
        super().write(key, data)


def receipt(version: str = "0.1.0~dev.1786300000+g0123456789ab") -> dict[str, object]:
    return {
        "channel": "dev",
        "distribution": "dev",
        "packages": {
            architecture: {
                "filename": f"vonk-forge-agent_{version}_{architecture}.deb",
                "sha256": hashlib.sha256(content).hexdigest(),
            }
            for architecture, content in PACKAGE_BYTES.items()
        },
        "snapshot": f"dev-{version}",
        "source_sha": SHA,
        "version": version,
    }


def test_publication_receipt_binds_the_arm64_package() -> None:
    state = load_state_module()
    version = "0.1.0~dev.1786300000+g0123456789ab"
    publication = {
        "channel": "dev",
        "distribution": "dev",
        "packages": {
            "arm64": {
                "filename": f"vonk-forge-agent_{version}_arm64.deb",
                "sha256": "b" * 64,
            },
        },
        "snapshot": f"dev-{version}",
        "source_sha": SHA,
        "version": version,
    }

    assert state._validate_receipt(publication) == publication


def bundles(tmp_path: Path, publication: dict[str, object]) -> tuple[bytes, bytes]:
    state = load_state_module()
    channel = publication["channel"]
    assert isinstance(channel, str)
    distribution, keyring = state.CHANNELS[channel]
    aptly = tmp_path / "aptly"
    aptly.mkdir()
    (aptly / "db").write_bytes(b"trusted aptly database")
    public = tmp_path / "public"
    for architecture in PACKAGE_BYTES:
        index = public / f"dists/{distribution}/main/binary-{architecture}"
        index.mkdir(parents=True)
        (index / "Packages").write_bytes(f"{architecture} package index".encode())
        (index / "Packages.gz").write_bytes(
            f"compressed {architecture} package index".encode()
        )
    (public / f"dists/{distribution}/Release").write_bytes(b"release metadata")
    (public / f"dists/{distribution}/Release.gpg").write_bytes(
        b"detached signature"
    )
    (public / f"dists/{distribution}/InRelease").write_bytes(b"signed-at-t1")
    (public / keyring).write_bytes(b"public key")
    package_root = public / "pool/main/v/vonk-forge-agent"
    package_root.mkdir(parents=True)
    packages = publication["packages"]
    assert isinstance(packages, dict)
    for architecture, content in PACKAGE_BYTES.items():
        package = packages[architecture]
        assert isinstance(package, dict)
        (package_root / package["filename"]).write_bytes(content)
    return (
        state.build_bundle(aptly, "state", publication),
        state.build_bundle(public, "public", publication),
    )


def package_files(tmp_path: Path, publication: dict[str, object]) -> Path:
    package_dir = tmp_path / "packages"
    package_dir.mkdir()
    packages = publication["packages"]
    assert isinstance(packages, dict)
    for architecture, content in PACKAGE_BYTES.items():
        package = packages[architecture]
        assert isinstance(package, dict)
        (package_dir / package["filename"]).write_bytes(content)
    return package_dir


def package_records(publication: dict[str, object]) -> set[tuple[str, str, str, str]]:
    packages = publication["packages"]
    assert isinstance(packages, dict)
    version = publication["version"]
    assert isinstance(version, str)
    return {
        (
            "vonk-forge-agent",
            version,
            architecture,
            package["sha256"],
        )
        for architecture, package in packages.items()
        if isinstance(package, dict)
    }


def compare_test_versions(left: str, right: str) -> int:
    def key(value: str) -> tuple[int, ...]:
        stable, marker, development = value.partition("~dev.")
        base = tuple(int(part) for part in stable.split("."))
        if not marker:
            return (*base, sys.maxsize)
        return (*base, int(development.split("+", 1)[0]))

    return (key(left) > key(right)) - (key(left) < key(right))


class FakeAptly:
    def __init__(
        self,
        repo: set[tuple[str, str, str, str]],
        snapshots: dict[str, set[tuple[str, str, str, str]]],
        packages: dict[str, tuple[str, str, str, str]],
    ) -> None:
        self.repo = set(repo)
        self.snapshots = {name: set(records) for name, records in snapshots.items()}
        self.packages = packages
        self.operations: list[tuple[str, ...]] = []
        self.cleanup_count = 0
        self.fail: Callable[[tuple[str, ...]], bool] = lambda _arguments: False

    def run(
        self, _config: Path, *arguments: str, allow_no_results: bool = False
    ) -> str:
        assert isinstance(allow_no_results, bool)
        self.operations.append(arguments)
        if self.fail(arguments):
            raise RuntimeError("injected aptly interruption")
        if arguments[:3] == ("repo", "remove", "vonk-forge-dev"):
            query = arguments[3]
            match = re.fullmatch(
                r"Name \(= vonk-forge-agent\), \$Version \(= '([^']+)'\)",
                query,
            )
            assert match is not None
            self.repo = {record for record in self.repo if record[1] != match[1]}
            return ""
        if arguments[:3] == ("repo", "remove", "vonk-forge"):
            query = arguments[3]
            match = re.fullmatch(
                r"Name \(= vonk-forge-agent\), \$Version \(= '([^']+)'\)",
                query,
            )
            assert match is not None
            self.repo = {record for record in self.repo if record[1] != match[1]}
            return ""
        if arguments[:3] in {
            ("repo", "add", "vonk-forge-dev"),
            ("repo", "add", "vonk-forge"),
        }:
            self.repo.add(self.packages[arguments[3]])
            return ""
        if arguments[:2] == ("repo", "search"):
            return self._format(self.repo)
        if arguments == ("snapshot", "list", "-raw"):
            return "".join(f"{name}\n" for name in sorted(self.snapshots))
        if arguments[:2] == ("snapshot", "search"):
            return self._format(self.snapshots[arguments[3]])
        if arguments[:2] == ("snapshot", "drop"):
            del self.snapshots[arguments[2]]
            return ""
        if arguments == ("db", "cleanup"):
            self.cleanup_count += 1
            return ""
        raise AssertionError(f"unexpected aptly command: {arguments!r}")

    @staticmethod
    def _format(records: set[tuple[str, str, str, str]]) -> str:
        return "".join("\t".join(record) + "\n" for record in sorted(records))


def fake_aptly(
    tmp_path: Path,
    publication: dict[str, object],
    *,
    repo: set[tuple[str, str, str, str]] | None = None,
    snapshots: dict[str, set[tuple[str, str, str, str]]] | None = None,
) -> tuple[Path, Path, FakeAptly]:
    config = tmp_path / "aptly.json"
    config.write_text("{}\n")
    package_dir = package_files(tmp_path, publication)
    packages = publication["packages"]
    assert isinstance(packages, dict)
    version = publication["version"]
    assert isinstance(version, str)
    paths = {
        str((package_dir / package["filename"]).resolve()): (
            "vonk-forge-agent",
            version,
            architecture,
            package["sha256"],
        )
        for architecture, package in packages.items()
        if isinstance(package, dict)
    }
    return (
        config,
        package_dir,
        FakeAptly(repo or set(), snapshots or {}, paths),
    )


def write_public_tree(
    tmp_path: Path,
    records: set[tuple[str, str, str, str]],
    distribution: str = "dev",
) -> Path:
    public = tmp_path / "public"
    for architecture in ("arm64",):
        paragraphs: list[str] = []
        for package, version, record_architecture, digest in sorted(records):
            if record_architecture != architecture:
                continue
            filename = (
                "pool/main/v/vonk-forge-agent/"
                f"vonk-forge-agent_{version}_{architecture}.deb"
            )
            package_path = public / filename
            package_path.parent.mkdir(parents=True, exist_ok=True)
            package_path.write_bytes(PACKAGE_BYTES[architecture])
            assert hashlib.sha256(package_path.read_bytes()).hexdigest() == digest
            paragraphs.append(
                "\n".join(
                    (
                        f"Package: {package}",
                        f"Version: {version}",
                        f"Architecture: {architecture}",
                        f"Filename: {filename}",
                        f"SHA256: {digest}",
                        "Description: test package",
                    )
                )
            )
        index = public / f"dists/{distribution}/main/binary-{architecture}/Packages"
        index.parent.mkdir(parents=True, exist_ok=True)
        index.write_text("\n\n".join(paragraphs) + "\n")
    return public


def test_development_compaction_makes_each_new_snapshot_exact_and_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = load_state_module()
    previous = receipt("0.1.0~dev.1786300000+g0123456789ab")
    current = receipt("0.1.0~dev.1786300001+g0123456789ab")
    old_records = package_records(previous)
    current_records = package_records(current)
    config, package_dir, aptly = fake_aptly(
        tmp_path,
        current,
        repo=old_records,
        snapshots={previous["snapshot"]: old_records},
    )
    monkeypatch.setattr(state, "_run_aptly", aptly.run)
    monkeypatch.setattr(state, "_compare_versions", compare_test_versions)

    state.compact_aptly_state(
        current, config, "vonk-forge-dev", package_dir, tmp_path / "public", "prepare"
    )
    assert aptly.repo == current_records
    assert aptly.snapshots == {previous["snapshot"]: old_records}
    assert (
        "repo",
        "remove",
        "vonk-forge-dev",
        f"Name (= vonk-forge-agent), $Version (= '{previous['version']}')",
    ) in aptly.operations

    aptly.snapshots[current["snapshot"]] = set(aptly.repo)
    public = write_public_tree(tmp_path, current_records)
    state.compact_aptly_state(
        current, config, "vonk-forge-dev", package_dir, public, "finalize"
    )

    assert aptly.repo == current_records
    assert aptly.snapshots == {current["snapshot"]: current_records}
    assert aptly.cleanup_count == 1
    assert ("snapshot", "drop", previous["snapshot"]) in aptly.operations
    drop = aptly.operations.index(("snapshot", "drop", previous["snapshot"]))
    cleanup = aptly.operations.index(("db", "cleanup"))
    assert drop < cleanup

    state.compact_aptly_state(
        current, config, "vonk-forge-dev", package_dir, public, "finalize"
    )
    assert aptly.snapshots == {current["snapshot"]: current_records}
    assert aptly.cleanup_count == 2


def test_development_compaction_workflow_retry_restores_prior_committed_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = load_state_module()
    publication = receipt()
    previous = receipt("0.1.0~dev.1786299999+g0123456789ab")
    old_records = package_records(previous)
    config, package_dir, aptly = fake_aptly(
        tmp_path,
        publication,
        repo=old_records,
        snapshots={previous["snapshot"]: old_records},
    )
    monkeypatch.setattr(state, "_run_aptly", aptly.run)
    monkeypatch.setattr(state, "_compare_versions", compare_test_versions)
    failures = 0

    def fail_first_add(arguments: tuple[str, ...]) -> bool:
        nonlocal failures
        if arguments[:3] == ("repo", "add", "vonk-forge-dev"):
            failures += 1
            return failures == 1
        return False

    aptly.fail = fail_first_add
    with pytest.raises(RuntimeError, match="injected aptly interruption"):
        state.compact_aptly_state(
            publication,
            config,
            "vonk-forge-dev",
            package_dir,
            tmp_path / "public",
            "prepare",
        )
    retry = FakeAptly(old_records, {previous["snapshot"]: old_records}, aptly.packages)
    monkeypatch.setattr(state, "_run_aptly", retry.run)
    state.compact_aptly_state(
        publication,
        config,
        "vonk-forge-dev",
        package_dir,
        tmp_path / "public",
        "prepare",
    )
    assert retry.repo == package_records(publication)


def test_development_compaction_fails_closed_on_cumulative_current_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = load_state_module()
    previous = receipt("0.1.0~dev.1786300000+g0123456789ab")
    current = receipt("0.1.0~dev.1786300001+g0123456789ab")
    old_records = package_records(previous)
    current_records = package_records(current)
    config, package_dir, aptly = fake_aptly(
        tmp_path,
        current,
        repo=current_records,
        snapshots={
            previous["snapshot"]: old_records,
            current["snapshot"]: old_records | current_records,
        },
    )
    monkeypatch.setattr(state, "_run_aptly", aptly.run)
    monkeypatch.setattr(state, "_compare_versions", compare_test_versions)

    with pytest.raises(state.StateError, match="current aptly snapshot"):
        state.compact_aptly_state(
            current,
            config,
            "vonk-forge-dev",
            package_dir,
            tmp_path / "public",
            "finalize",
        )

    assert aptly.cleanup_count == 0
    assert previous["snapshot"] in aptly.snapshots


def stable_receipt(version: str) -> dict[str, object]:
    return {
        "channel": "stable",
        "distribution": "stable",
        "packages": {
            architecture: {
                "filename": f"vonk-forge-agent_{version}_{architecture}.deb",
                "sha256": hashlib.sha256(content).hexdigest(),
            }
            for architecture, content in PACKAGE_BYTES.items()
        },
        "snapshot": f"stable-{version}",
        "source_sha": SHA,
        "version": version,
    }


def test_stable_compaction_retains_current_and_two_complete_predecessors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = load_state_module()
    historical = [stable_receipt(version) for version in ("1.8.0", "1.9.0", "1.10.0")]
    publication = stable_receipt("1.11.0")
    old_records = set().union(*(package_records(item) for item in historical))
    old_snapshots = {
        item["snapshot"]: package_records(item) for item in historical
    }
    config, package_dir, aptly = fake_aptly(
        tmp_path,
        publication,
        repo=old_records,
        snapshots=old_snapshots,
    )
    monkeypatch.setattr(state, "_run_aptly", aptly.run)
    monkeypatch.setattr(state, "_compare_versions", compare_test_versions)

    state.compact_aptly_state(
        publication,
        config,
        "vonk-forge",
        package_dir,
        tmp_path / "public",
        "prepare",
    )
    assert {record[1] for record in aptly.repo} == {"1.9.0", "1.10.0", "1.11.0"}
    assert {record[2] for record in aptly.repo} == {"arm64"}
    aptly.snapshots[publication["snapshot"]] = set(aptly.repo)
    public = write_public_tree(tmp_path, aptly.repo, "stable")
    state.compact_aptly_state(
        publication, config, "vonk-forge", package_dir, public, "finalize"
    )
    assert aptly.snapshots == {publication["snapshot"]: aptly.repo}
    assert aptly.cleanup_count == 1


def test_stable_compaction_rejects_rollback_before_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = load_state_module()
    high = stable_receipt("2.0.0")
    publication = stable_receipt("1.9.0")
    high_records = package_records(high)
    config, package_dir, aptly = fake_aptly(
        tmp_path,
        publication,
        repo=high_records,
        snapshots={high["snapshot"]: high_records},
    )
    monkeypatch.setattr(state, "_run_aptly", aptly.run)
    monkeypatch.setattr(state, "_compare_versions", compare_test_versions)

    with pytest.raises(state.StateError, match="roll back"):
        state.compact_aptly_state(
            publication,
            config,
            "vonk-forge",
            package_dir,
            tmp_path / "public",
            "prepare",
        )
    assert not any(operation[:2] == ("repo", "remove") for operation in aptly.operations)


def test_repository_version_rejects_an_extra_architecture() -> None:
    state = load_state_module()
    publication = stable_receipt("1.2.3")
    records = package_records(publication)
    records.add(("vonk-forge-agent", "1.2.3", "amd64", "f" * 64))

    with pytest.raises(state.StateError, match="incomplete package version"):
        state._group_complete_versions(records, "stable")


def test_repository_version_rejects_incomplete_and_unexpected_packages() -> None:
    state = load_state_module()
    records = {("vonk-forge-agent", "1.2.3", "amd64", "a" * 64)}
    with pytest.raises(state.StateError, match="incomplete package version"):
        state._group_complete_versions(records, "stable")

    with pytest.raises(state.StateError, match="package identity"):
        state._group_complete_versions(
            {("another-package", "1.2.3", "arm64", "a" * 64)}, "stable"
        )


def test_public_package_matrix_requires_every_retained_version_and_architecture(
    tmp_path: Path,
) -> None:
    state = load_state_module()
    publication = stable_receipt("1.2.0")
    predecessor = stable_receipt("1.1.0")
    records = package_records(publication) | package_records(predecessor)
    public = write_public_tree(tmp_path, records, "stable")
    index = public / "dists/stable/main/binary-arm64/Packages"
    paragraphs = index.read_text().strip().split("\n\n")
    index.write_text(paragraphs[-1] + "\n")

    assert state._public_package_records(publication, public) != records


def test_bundle_size_limit_reports_kind_limit_and_observed_bytes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = load_state_module()
    monkeypatch.setattr(state, "MAX_BUNDLE_BYTES", 3)

    with pytest.raises(
        state.StateError,
        match=r"state bundle exceeds 3 byte limit: 4 bytes",
    ):
        state.validate_bundle(b"1234", "state")


def without_bundle_member(raw: bytes, missing_name: str) -> bytes:
    output = io.BytesIO()
    with (
        tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as source,
        tarfile.open(fileobj=output, mode="w:gz") as target,
    ):
        for member in source.getmembers():
            if member.name == missing_name:
                continue
            stream = source.extractfile(member) if member.isfile() else None
            target.addfile(member, stream)
    return output.getvalue()


def test_partial_immutable_objects_are_completable_and_conflicts_fail(
    tmp_path: Path,
) -> None:
    state = load_state_module()
    publication = receipt()
    state_bundle, public_bundle = bundles(tmp_path, publication)
    operations: list[str] = []
    prefix = f"arm64/versions/{publication['version']}"
    private = FakeR2(
        "state", operations, fail_once={f"{prefix}/commit.json"}
    )

    with pytest.raises(OSError, match="injected R2 failure"):
        state.commit_candidate(private, publication, state_bundle, public_bundle)

    assert set(private.objects) == {
        f"{prefix}/aptly-state.tar.gz",
        f"{prefix}/public-tree.tar.gz",
    }

    manifest = state.commit_candidate(
        private, publication, state_bundle, public_bundle
    )

    assert private.objects[f"{prefix}/commit.json"] == manifest
    commit_failure = operations.index(f"state:write-failed:{prefix}/commit.json")
    for key in ("aptly-state.tar.gz", "public-tree.tar.gz"):
        assert operations.index(f"state:write:{prefix}/{key}") < commit_failure
    writes = [operation for operation in operations if ":write:" in operation]
    assert writes[-1] == f"state:write:{prefix}/commit.json"

    (tmp_path / "aptly/db").write_bytes(b"different aptly database")
    changed = state.build_bundle(tmp_path / "aptly", "state", publication)
    with pytest.raises(state.StateError, match="immutable object conflict"):
        state.commit_candidate(private, publication, changed, public_bundle)


def test_candidate_data_objects_upload_concurrently_before_commit(
    tmp_path: Path,
) -> None:
    state = load_state_module()
    publication = receipt()
    state_bundle, public_bundle = bundles(tmp_path, publication)
    operations: list[str] = []
    private = ConcurrentBundleR2("state", operations)

    state.commit_candidate(private, publication, state_bundle, public_bundle)

    prefix = f"arm64/versions/{publication['version']}"
    commit = operations.index(f"state:write:{prefix}/commit.json")
    state_object = operations.index(f"state:write:{prefix}/aptly-state.tar.gz")
    public_object = operations.index(f"state:write:{prefix}/public-tree.tar.gz")
    assert max(state_object, public_object) < commit


def test_single_partial_data_object_is_completed_on_exact_retry(tmp_path: Path) -> None:
    state = load_state_module()
    publication = receipt()
    state_bundle, public_bundle = bundles(tmp_path, publication)
    operations: list[str] = []
    prefix = f"arm64/versions/{publication['version']}"
    private = FakeR2(
        "state",
        operations,
        fail_once={f"{prefix}/public-tree.tar.gz"},
    )

    with pytest.raises(OSError, match="injected R2 failure"):
        state.commit_candidate(private, publication, state_bundle, public_bundle)
    assert set(private.objects) == {f"{prefix}/aptly-state.tar.gz"}

    state.commit_candidate(private, publication, state_bundle, public_bundle)

    assert set(private.objects) == {
        f"{prefix}/aptly-state.tar.gz",
        f"{prefix}/public-tree.tar.gz",
        f"{prefix}/commit.json",
    }


def test_commit_requires_persisted_data_hashes_to_match(tmp_path: Path) -> None:
    state = load_state_module()
    publication = receipt()
    state_bundle, public_bundle = bundles(tmp_path, publication)
    operations: list[str] = []
    prefix = f"arm64/versions/{publication['version']}"
    private = FakeR2(
        "state",
        operations,
        corrupt_once={f"{prefix}/aptly-state.tar.gz"},
    )

    with pytest.raises(state.StateError, match="persisted object hash mismatch"):
        state.commit_candidate(private, publication, state_bundle, public_bundle)

    assert f"{prefix}/commit.json" not in private.objects


def test_committed_manifest_is_the_monotonic_high_water_without_latest(
    tmp_path: Path,
) -> None:
    state = load_state_module()
    operations: list[str] = []
    private = FakeR2("state", operations)
    high = receipt("0.1.0~dev.1786300002+g0123456789ab")
    state_bundle, public_bundle = bundles(tmp_path, high)
    state.commit_candidate(private, high, state_bundle, public_bundle)
    assert "latest.json" not in private.objects

    older = receipt("0.1.0~dev.1786300001+g0123456789ab")
    with pytest.raises(state.StateError, match="roll back"):
        state.prepare_candidate(private, older)


def test_new_arm64_epoch_ignores_legacy_state_objects() -> None:
    state = load_state_module()
    operations: list[str] = []
    private = FakeR2("state", operations)
    private.objects["versions/legacy/commit.json"] = b"incompatible historical receipt"

    publication = receipt("0.1.0~dev.1786300000+g0123456789ab")
    prepared = state.prepare_candidate(private, publication)

    assert prepared.mode == "pending"
    assert "state:list:versions/" not in operations
    assert "state:read:versions/legacy/commit.json" not in operations


def test_new_arm64_epoch_preserves_global_publication_high_water() -> None:
    state = load_state_module()
    operations: list[str] = []
    private = FakeR2("state", operations)
    historical_version = "0.1.0~dev.1786300001+g0123456789ab"
    private.objects["latest.json"] = state.canonical_json(
        {
            "channel": "dev",
            "commit": f"versions/{historical_version}/commit.json",
            "commit_sha256": "a" * 64,
            "version": historical_version,
        }
    )

    with pytest.raises(state.StateError, match="publication epoch"):
        state.prepare_candidate(
            private,
            receipt("0.1.0~dev.1786300000+g0123456789ab"),
        )

    assert "versions/legacy/commit.json" not in private.objects


def test_pending_prepare_only_downloads_predecessor_aptly_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = load_state_module()
    monkeypatch.setattr(state, "_compare_versions", compare_test_versions)
    operations: list[str] = []
    private = FakeR2("state", operations)
    previous = receipt("0.1.0~dev.1786300000+g0123456789ab")
    current = receipt("0.1.0~dev.1786300001+g0123456789ab")
    previous_root = tmp_path / "previous"
    previous_root.mkdir()
    state_bundle, public_bundle = bundles(previous_root, previous)
    state.commit_candidate(private, previous, state_bundle, public_bundle)
    operations.clear()

    prepared = state.prepare_candidate(private, current)

    prefix = f"arm64/versions/{previous['version']}"
    assert prepared.state_bundle == state_bundle
    assert f"state:read:{prefix}/aptly-state.tar.gz" in operations
    assert f"state:read:{prefix}/public-tree.tar.gz" not in operations


def test_prepare_scans_committed_manifests_concurrently(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = load_state_module()
    monkeypatch.setattr(state, "_compare_versions", compare_test_versions)
    operations: list[str] = []
    private = ConcurrentReadR2(
        "state", operations, concurrent_read_count=state.MAX_OBJECT_STORE_WORKERS
    )
    committed = tuple(
        receipt(f"0.1.0~dev.{1786300000 + index}+g0123456789ab")
        for index in range(state.MAX_OBJECT_STORE_WORKERS)
    )
    for index, publication in enumerate(committed):
        root = tmp_path / f"committed-{index}"
        root.mkdir()
        state_bundle, public_bundle = bundles(root, publication)
        state.commit_candidate(private, publication, state_bundle, public_bundle)
    private.concurrent_reads = {
        f"arm64/versions/{publication['version']}/commit.json"
        for publication in committed
    }

    prepared = state.prepare_candidate(
        private,
        receipt("0.1.0~dev.1786300008+g0123456789ab"),
    )

    assert prepared.high_water_version == committed[-1]["version"]


def test_concurrent_manifest_scan_rejects_corruption_before_bundle_reads(
    tmp_path: Path,
) -> None:
    state = load_state_module()
    operations: list[str] = []
    private = ConcurrentReadR2("state", operations)
    committed = (
        receipt("0.1.0~dev.1786300000+g0123456789ab"),
        receipt("0.1.0~dev.1786300001+g0123456789ab"),
    )
    commit_keys: set[str] = set()
    for index, publication in enumerate(committed):
        root = tmp_path / f"corrupt-{index}"
        root.mkdir()
        state_bundle, public_bundle = bundles(root, publication)
        state.commit_candidate(private, publication, state_bundle, public_bundle)
        commit_keys.add(f"arm64/versions/{publication['version']}/commit.json")
    private.objects[max(commit_keys)] = b"{}\n"
    private.concurrent_reads = commit_keys
    operations.clear()

    with pytest.raises(state.StateError, match="commit manifest is invalid"):
        state.prepare_candidate(
            private,
            receipt("0.1.0~dev.1786300002+g0123456789ab"),
        )

    assert operations[0] == "state:list:arm64/versions/"
    assert set(operations[1:]) == {
        f"state:read:{key}" for key in commit_keys
    }


def test_equal_replay_downloads_committed_bundles_concurrently(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = load_state_module()
    monkeypatch.setattr(state, "_compare_versions", compare_test_versions)
    publication = receipt()
    state_bundle, public_bundle = bundles(tmp_path, publication)
    operations: list[str] = []
    private = ConcurrentReadR2("state", operations)
    state.commit_candidate(private, publication, state_bundle, public_bundle)
    prefix = f"arm64/versions/{publication['version']}"
    private.concurrent_reads = {
        f"{prefix}/aptly-state.tar.gz",
        f"{prefix}/public-tree.tar.gz",
    }

    prepared = state.prepare_candidate(private, publication)

    assert prepared.mode == "committed"
    assert prepared.state_bundle == state_bundle
    assert prepared.public_bundle == public_bundle


def test_commit_precedes_public_bytes_and_single_latest_pointer(
    tmp_path: Path,
) -> None:
    state = load_state_module()
    publication = receipt()
    state_bundle, public_bundle = bundles(tmp_path, publication)
    operations: list[str] = []
    private = FakeR2("state", operations)
    public = FakeR2("public", operations)

    state.commit_candidate(private, publication, state_bundle, public_bundle)
    state.publish_committed(private, public, publication)

    prefix = f"arm64/versions/{publication['version']}"
    commit_index = operations.index(f"state:write:{prefix}/commit.json")
    public_indexes = [
        index
        for index, operation in enumerate(operations)
        if operation.startswith("public:write:")
    ]
    latest_index = operations.index("state:write:latest.json")
    assert commit_index < min(public_indexes) < latest_index
    assert not any(key.startswith("latest/") for key in private.objects)
    pointer = state.parse_canonical_json(private.objects["latest.json"])
    assert pointer == {
        "channel": "dev",
        "commit": f"{prefix}/commit.json",
        "commit_sha256": state.sha256(private.objects[f"{prefix}/commit.json"]),
        "version": publication["version"],
    }


def test_publication_only_downloads_committed_public_tree(tmp_path: Path) -> None:
    state = load_state_module()
    publication = receipt()
    state_bundle, public_bundle = bundles(tmp_path, publication)
    operations: list[str] = []
    private = FakeR2("state", operations)
    public = FakeR2("public", operations)
    state.commit_candidate(private, publication, state_bundle, public_bundle)
    operations.clear()

    state.publish_committed(private, public, publication)

    prefix = f"arm64/versions/{publication['version']}"
    assert f"state:read:{prefix}/public-tree.tar.gz" in operations
    assert f"state:read:{prefix}/aptly-state.tar.gz" not in operations


def test_inrelease_is_the_final_public_commit_after_all_other_public_objects(
    tmp_path: Path,
) -> None:
    state = load_state_module()
    publication = receipt()
    state_bundle, public_bundle = bundles(tmp_path, publication)
    operations: list[str] = []
    private = FakeR2("state", operations)
    public = FakeR2("public", operations)

    state.commit_candidate(private, publication, state_bundle, public_bundle)
    state.publish_committed(private, public, publication)

    inrelease = operations.index("public:write:dists/dev/InRelease")
    release = operations.index("public:write:dists/dev/Release")
    release_gpg = operations.index("public:write:dists/dev/Release.gpg")
    latest = operations.index("state:write:latest.json")
    public_writes = [
        index
        for index, operation in enumerate(operations)
        if operation.startswith("public:write:")
    ]
    release_writes = {release, release_gpg, inrelease}
    data_writes = [index for index in public_writes if index not in release_writes]
    assert all(index < release and index < release_gpg for index in data_writes)
    assert all(index < inrelease for index in public_writes if index != inrelease)
    assert release < inrelease
    assert release_gpg < inrelease
    assert inrelease < latest


def test_public_objects_upload_concurrently_within_commit_phases(
    tmp_path: Path,
) -> None:
    state = load_state_module()
    publication = receipt()
    state_bundle, public_bundle = bundles(tmp_path, publication)
    operations: list[str] = []
    private = FakeR2("state", operations)
    public = ConcurrentPublicR2("public", operations)

    state.commit_candidate(private, publication, state_bundle, public_bundle)
    state.publish_committed(private, public, publication)

    inrelease = operations.index("public:write:dists/dev/InRelease")
    latest = operations.index("state:write:latest.json")
    assert operations.index("public:write:dists/dev/Release") < inrelease
    assert operations.index("public:write:dists/dev/Release.gpg") < inrelease
    assert inrelease < latest


@pytest.mark.parametrize(
    "failed_key",
    (
        "vonk-forge-dev-archive-keyring.gpg",
        (
            "pool/main/v/vonk-forge-agent/"
            "vonk-forge-agent_0.1.0~dev.1786300000+g0123456789ab_arm64.deb"
        ),
        "dists/dev/Release",
        "dists/dev/Release.gpg",
        "dists/dev/InRelease",
    ),
)
def test_public_failure_before_or_at_inrelease_never_advances_latest(
    tmp_path: Path, failed_key: str
) -> None:
    state = load_state_module()
    publication = receipt()
    state_bundle, public_bundle = bundles(tmp_path, publication)
    operations: list[str] = []
    private = FakeR2("state", operations)
    public = FakeR2("public", operations, fail_once={failed_key})
    state.commit_candidate(private, publication, state_bundle, public_bundle)

    with pytest.raises(OSError, match="injected R2 failure"):
        state.publish_committed(private, public, publication)

    assert "latest.json" not in private.objects
    assert "state:write:latest.json" not in operations


def test_ordinary_object_failure_stops_before_release_commit_metadata(
    tmp_path: Path,
) -> None:
    state = load_state_module()
    publication = receipt()
    state_bundle, public_bundle = bundles(tmp_path, publication)
    operations: list[str] = []
    private = FakeR2("state", operations)
    failed_key = "vonk-forge-dev-archive-keyring.gpg"
    public = FakeR2("public", operations, fail_once={failed_key})
    state.commit_candidate(private, publication, state_bundle, public_bundle)

    with pytest.raises(OSError, match="injected R2 failure"):
        state.publish_committed(private, public, publication)

    assert "dists/dev/Release" not in public.objects
    assert "dists/dev/Release.gpg" not in public.objects
    assert "dists/dev/InRelease" not in public.objects
    assert "latest.json" not in private.objects


def test_equal_replay_publishes_persisted_public_bytes_without_regeneration(
    tmp_path: Path,
) -> None:
    state = load_state_module()
    publication = receipt()
    state_bundle, public_bundle = bundles(tmp_path, publication)
    operations: list[str] = []
    private = FakeR2("state", operations)
    public = FakeR2("public", operations)
    state.commit_candidate(private, publication, state_bundle, public_bundle)
    state.publish_committed(private, public, publication)

    prepared = state.prepare_candidate(private, publication)
    assert prepared.mode == "committed"
    assert prepared.public_bundle == public_bundle

    regenerated = tmp_path / "regenerated/public"
    (regenerated / "dists/dev").mkdir(parents=True)
    (regenerated / "dists/dev/Release").write_bytes(b"release metadata at t2")
    (regenerated / "dists/dev/Release.gpg").write_bytes(b"detached signature at t2")
    (regenerated / "dists/dev/InRelease").write_bytes(b"signed-at-t2")
    (regenerated / "vonk-forge-dev-archive-keyring.gpg").write_bytes(b"public key")
    package_root = regenerated / "pool/main/v/vonk-forge-agent"
    package_root.mkdir(parents=True)
    packages = publication["packages"]
    assert isinstance(packages, dict)
    for architecture, content in PACKAGE_BYTES.items():
        package = packages[architecture]
        assert isinstance(package, dict)
        (package_root / package["filename"]).write_bytes(content)
    assert state.build_bundle(regenerated, "public", publication) != public_bundle

    state.publish_committed(private, public, publication)

    assert public.objects["dists/dev/InRelease"] == b"signed-at-t1"


def test_public_failure_does_not_advance_latest_and_exact_retry_completes(
    tmp_path: Path,
) -> None:
    state = load_state_module()
    publication = receipt()
    state_bundle, public_bundle = bundles(tmp_path, publication)
    operations: list[str] = []
    private = FakeR2("state", operations)
    public = FakeR2(
        "public", operations, fail_once={"dists/dev/InRelease"}
    )
    state.commit_candidate(private, publication, state_bundle, public_bundle)

    with pytest.raises(OSError, match="injected R2 failure"):
        state.publish_committed(private, public, publication)
    assert "latest.json" not in private.objects

    state.publish_committed(private, public, publication)

    assert public.objects["dists/dev/InRelease"] == b"signed-at-t1"
    assert "latest.json" in private.objects


def test_successor_publication_preserves_older_public_pool_objects(
    tmp_path: Path,
) -> None:
    state = load_state_module()
    operations: list[str] = []
    private = FakeR2("state", operations)
    public = FakeR2("public", operations)
    first = receipt("0.1.0~dev.1786300000+g0123456789ab")
    second = receipt("0.1.0~dev.1786300001+g0123456789ab")
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    first_root.mkdir()
    second_root.mkdir()
    first_state, first_public = bundles(first_root, first)
    second_state, second_public = bundles(second_root, second)

    state.commit_candidate(private, first, first_state, first_public)
    state.publish_committed(private, public, first)
    first_package = first["packages"]["arm64"]
    assert isinstance(first_package, dict)
    first_key = f"pool/main/v/vonk-forge-agent/{first_package['filename']}"
    immutable_bytes = public.objects[first_key]

    state.commit_candidate(private, second, second_state, second_public)
    state.publish_committed(private, public, second)

    assert public.objects[first_key] == immutable_bytes


def test_stable_successor_replays_predecessor_pool_object_immutably(
    tmp_path: Path,
) -> None:
    state = load_state_module()
    operations: list[str] = []
    private = FakeR2("state", operations)
    public = FakeR2("public", operations)
    first = stable_receipt("1.0.0")
    second = stable_receipt("1.1.0")
    first_root = tmp_path / "first-stable"
    second_root = tmp_path / "second-stable"
    first_root.mkdir()
    second_root.mkdir()
    first_state, first_public = bundles(first_root, first)
    second_state, _ = bundles(second_root, second)
    first_package = first["packages"]["arm64"]
    assert isinstance(first_package, dict)
    predecessor = second_root / "public/pool/main/v/vonk-forge-agent"
    (predecessor / first_package["filename"]).write_bytes(PACKAGE_BYTES["arm64"])
    second_public = state.build_bundle(second_root / "public", "public", second)

    state.commit_candidate(private, first, first_state, first_public)
    state.publish_committed(private, public, first)
    first_key = f"pool/main/v/vonk-forge-agent/{first_package['filename']}"
    immutable_bytes = public.objects[first_key]
    state.commit_candidate(private, second, second_state, second_public)
    state.publish_committed(private, public, second)

    assert public.objects[first_key] == immutable_bytes
    assert operations.count(f"public:read:{first_key}") >= 2


def test_immutable_public_conflict_preserves_all_public_bytes_and_latest(
    tmp_path: Path,
) -> None:
    state = load_state_module()
    operations: list[str] = []
    private = FakeR2("state", operations)
    public = FakeR2("public", operations)
    first = stable_receipt("1.0.0")
    second = stable_receipt("1.1.0")
    first_root = tmp_path / "conflict-first"
    second_root = tmp_path / "conflict-second"
    first_root.mkdir()
    second_root.mkdir()
    first_state, first_public = bundles(first_root, first)
    second_state, _ = bundles(second_root, second)
    first_package = first["packages"]["arm64"]
    assert isinstance(first_package, dict)
    predecessor = second_root / "public/pool/main/v/vonk-forge-agent"
    (predecessor / first_package["filename"]).write_bytes(PACKAGE_BYTES["arm64"])
    second_public = state.build_bundle(second_root / "public", "public", second)
    state.commit_candidate(private, first, first_state, first_public)
    state.publish_committed(private, public, first)
    state.commit_candidate(private, second, second_state, second_public)
    first_key = f"pool/main/v/vonk-forge-agent/{first_package['filename']}"
    public.objects[first_key] = b"pre-existing conflicting bytes"
    before_public = dict(public.objects)
    before_latest = private.objects["latest.json"]

    with pytest.raises(state.StateError, match="immutable public object conflict"):
        state.publish_committed(private, public, second)

    assert public.objects == before_public
    assert private.objects["latest.json"] == before_latest


def test_public_bundle_binds_the_exact_verified_package_hash(tmp_path: Path) -> None:
    state = load_state_module()
    publication = receipt()
    packages = publication["packages"]
    assert isinstance(packages, dict)
    arm64 = packages["arm64"]
    assert isinstance(arm64, dict)
    arm64["sha256"] = "b" * 64

    with pytest.raises(state.StateError, match="public package hash"):
        bundles(tmp_path, publication)


@pytest.mark.parametrize(
    "missing_name",
    (
        "public/dists/dev/Release",
        "public/dists/dev/Release.gpg",
    ),
)
def test_public_bundle_requires_exact_release_metadata_objects(
    tmp_path: Path, missing_name: str
) -> None:
    state = load_state_module()
    publication = receipt()
    _, public_bundle = bundles(tmp_path, publication)

    with pytest.raises(state.StateError, match="public bundle is incomplete"):
        state.validate_bundle(
            without_bundle_member(public_bundle, missing_name),
            "public",
            publication,
        )


def test_public_bundle_excludes_valid_aptly_by_hash_aliases(tmp_path: Path) -> None:
    state = load_state_module()
    publication = receipt()
    bundles(tmp_path, publication)
    index = tmp_path / "public/dists/dev/main/binary-arm64/Packages"
    digest = hashlib.sha256(index.read_bytes()).hexdigest()
    by_hash = index.parent / "by-hash/SHA256"
    by_hash.mkdir(parents=True)
    digest_index = by_hash / digest
    digest_index.write_bytes(index.read_bytes())
    alias = by_hash / "Packages"
    alias.symlink_to(digest_index)

    raw = state.build_bundle(tmp_path / "public", "public", publication)
    validated = state.validate_bundle(raw, "public", publication)

    assert f"public/{digest_index.relative_to(tmp_path / 'public')}" in validated.files
    assert f"public/{alias.relative_to(tmp_path / 'public')}" not in validated.files


def test_public_bundle_rejects_by_hash_alias_outside_its_digest_directory(
    tmp_path: Path,
) -> None:
    state = load_state_module()
    publication = receipt()
    bundles(tmp_path, publication)
    by_hash = tmp_path / "public/dists/dev/main/binary-arm64/by-hash/SHA256"
    by_hash.mkdir(parents=True)
    (by_hash / "Packages").symlink_to(tmp_path / "public/dists/dev/Release")

    with pytest.raises(state.StateError, match="bundle source is unsafe"):
        state.build_bundle(tmp_path / "public", "public", publication)


@pytest.mark.parametrize(
    "names",
    (
        ("aptly", "aptly/db", "aptly/./db", "publication-receipt.json"),
        ("aptly", "aptly/db", "aptly//db", "publication-receipt.json"),
        ("aptly", "aptly/db", "aptly/../db", "publication-receipt.json"),
        ("aptly", "aptly/db", "/aptly/db", "publication-receipt.json"),
        ("aptly", "aptly/db", "aptly/control\nname", "publication-receipt.json"),
        ("aptly", "aptly/db", "outside/file", "publication-receipt.json"),
    ),
)
def test_structural_tar_validation_rejects_noncanonical_destinations(
    names: tuple[str, ...],
) -> None:
    state = load_state_module()
    publication = receipt()
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name in names:
            member = tarfile.TarInfo(name)
            if name in {"aptly", "aptly/db"}:
                member.type = tarfile.DIRTYPE
                archive.addfile(member)
            else:
                raw = state.canonical_json(publication) if name == "publication-receipt.json" else b"x"
                member.size = len(raw)
                archive.addfile(member, io.BytesIO(raw))

    with pytest.raises(state.StateError, match="unsafe archive member"):
        state.validate_bundle(buffer.getvalue(), "state", publication)


@pytest.mark.parametrize(
    "member_type",
    (
        tarfile.SYMTYPE,
        tarfile.LNKTYPE,
        tarfile.CHRTYPE,
        tarfile.BLKTYPE,
        tarfile.FIFOTYPE,
    ),
)
def test_structural_tar_validation_rejects_links_devices_and_fifos(
    member_type: bytes,
) -> None:
    state = load_state_module()
    publication = receipt()
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        root = tarfile.TarInfo("aptly")
        root.type = tarfile.DIRTYPE
        archive.addfile(root)
        unsafe = tarfile.TarInfo("aptly/unsafe")
        unsafe.type = member_type
        unsafe.linkname = "aptly/db"
        archive.addfile(unsafe)
        raw = state.canonical_json(publication)
        receipt_member = tarfile.TarInfo("publication-receipt.json")
        receipt_member.size = len(raw)
        archive.addfile(receipt_member, io.BytesIO(raw))

    with pytest.raises(state.StateError, match="unsafe archive member"):
        state.validate_bundle(buffer.getvalue(), "state", publication)
