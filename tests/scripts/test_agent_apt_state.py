from __future__ import annotations

import hashlib
import io
import sys
import tarfile
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/agent-apt-state"
SHA = "0123456789abcdef0123456789abcdef01234567"
PACKAGE_BYTES = {"amd64": b"amd64 package bytes", "arm64": b"arm64 package bytes"}


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


def test_publication_receipt_binds_both_architecture_packages() -> None:
    state = load_state_module()
    version = "0.1.0~dev.1786300000+g0123456789ab"
    publication = {
        "channel": "dev",
        "distribution": "dev",
        "packages": {
            "amd64": {
                "filename": f"vonk-forge-agent_{version}_amd64.deb",
                "sha256": "a" * 64,
            },
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
    aptly = tmp_path / "aptly"
    aptly.mkdir()
    (aptly / "db").write_bytes(b"trusted aptly database")
    public = tmp_path / "public"
    for architecture in PACKAGE_BYTES:
        index = public / f"dists/dev/main/binary-{architecture}"
        index.mkdir(parents=True)
        (index / "Packages").write_bytes(f"{architecture} package index".encode())
        (index / "Packages.gz").write_bytes(
            f"compressed {architecture} package index".encode()
        )
    (public / "dists/dev/Release").write_bytes(b"release metadata")
    (public / "dists/dev/Release.gpg").write_bytes(b"detached signature")
    (public / "dists/dev/InRelease").write_bytes(b"signed-at-t1")
    (public / "vonk-forge-dev-archive-keyring.gpg").write_bytes(b"public key")
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
    prefix = f"versions/{publication['version']}"
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
    assert operations.index(f"state:write:{prefix}/aptly-state.tar.gz") < operations.index(
        f"state:write:{prefix}/public-tree.tar.gz"
    )
    assert operations.index(f"state:write:{prefix}/public-tree.tar.gz") < operations.index(
        f"state:write-failed:{prefix}/commit.json"
    )
    writes = [operation for operation in operations if ":write:" in operation]
    assert writes[-1] == f"state:write:{prefix}/commit.json"

    (tmp_path / "aptly/db").write_bytes(b"different aptly database")
    changed = state.build_bundle(tmp_path / "aptly", "state", publication)
    with pytest.raises(state.StateError, match="immutable object conflict"):
        state.commit_candidate(private, publication, changed, public_bundle)


def test_single_partial_data_object_is_completed_on_exact_retry(tmp_path: Path) -> None:
    state = load_state_module()
    publication = receipt()
    state_bundle, public_bundle = bundles(tmp_path, publication)
    operations: list[str] = []
    prefix = f"versions/{publication['version']}"
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
    prefix = f"versions/{publication['version']}"
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

    prefix = f"versions/{publication['version']}"
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
