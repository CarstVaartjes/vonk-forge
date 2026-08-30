import argparse
import hashlib
import importlib.machinery
import importlib.util
import json
import subprocess
from pathlib import Path
from typing import Self

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/repair-capsule-publication"
LOADER = importlib.machinery.SourceFileLoader("repair_capsule_publication", str(SCRIPT))
SPEC = importlib.util.spec_from_loader(LOADER.name, LOADER)
assert SPEC is not None and SPEC.loader is not None
PUBLICATION = importlib.util.module_from_spec(SPEC)
LOADER.exec_module(PUBLICATION)

NODE_ID = "spk_2818d189042b4c77aefa7796f4befd23"
AUTHORITY = "a" * 64
BINARY_SOURCE = "a122909feaa3" + "1" * 28
PACKAGING_SOURCE = "b" * 40
RELEASE_KEY = "f" * 64
VERSION = "0.1.0~dev.381+ga122909feaa3+repair.spk2818d189042b4c77aefa7796f4befd23.1"
SIGNATURE = "c" * 128
TARGET_BINARY = "d" * 64
TARGET_BUILD = f"sha256:{'e' * 64}"


def _canonical(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _verified(raw: bytes, **overrides: object) -> dict[str, object]:
    result: dict[str, object] = {
        "architecture": "arm64",
        "binary_source_revision": BINARY_SOURCE,
        "ok": True,
        "package": "vonk-forge-agent",
        "package_bytes": len(raw),
        "package_signature": SIGNATURE,
        "packaging_source_revision": PACKAGING_SOURCE,
        "repair_authority_sha256": AUTHORITY,
        "repair_node_id": NODE_ID,
        "release_key_sha256": RELEASE_KEY,
        "sha256": hashlib.sha256(raw).hexdigest(),
        "target_binary_digest": TARGET_BINARY,
        "target_build_digest": TARGET_BUILD,
        "version": VERSION,
    }
    result.update(overrides)
    return result


def _authority_args() -> dict[str, str]:
    return {
        "expected_binary_source_revision": BINARY_SOURCE,
        "expected_packaging_source_revision": PACKAGING_SOURCE,
        "expected_release_key_sha256": RELEASE_KEY,
    }


def _assemble(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, bytes]:
    raw = b"signed node-bound repair deb\n"
    deb = tmp_path / "repair.deb"
    deb.write_bytes(raw)
    deb.chmod(0o644)
    monkeypatch.setattr(
        PUBLICATION,
        "_invoke_verifier",
        lambda *_arguments: _verified(raw),
    )
    bundle = tmp_path / "bundle"
    PUBLICATION.assemble(
        argparse.Namespace(
            deb=deb,
            expected_node_id=NODE_ID,
            expected_authority_sha256=AUTHORITY,
            output=bundle,
            **_authority_args(),
        )
    )
    return bundle, raw


def _bundle_files(bundle: Path) -> tuple[dict[str, object], Path, Path]:
    plan = json.loads((bundle / "publication-plan.json").read_text())
    package = bundle / "objects" / plan["objects"][0]["key"]
    manifest = bundle / "objects" / plan["objects"][1]["key"]
    return plan, package, manifest


def test_assemble_derives_exact_manifest_and_provenance_from_verifier(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle, raw = _assemble(tmp_path, monkeypatch)
    plan, package_path, manifest_path = _bundle_files(bundle)
    package_sha256 = hashlib.sha256(raw).hexdigest()
    prefix = f"repair-capsules/{NODE_ID}/{AUTHORITY}/{package_sha256}"

    assert package_path.read_bytes() == raw
    assert plan == {
        "authority_sha256": AUTHORITY,
        "binary_source_revision": BINARY_SOURCE,
        "kind": "agent-repair-capsule-publication",
        "node_id": NODE_ID,
        "objects": [
            {
                "key": f"{prefix}/vonk-forge-agent.deb",
                "kind": "package",
                "sha256": package_sha256,
                "size": len(raw),
            },
            {
                "key": f"{prefix}/manifest.json",
                "kind": "manifest",
                "sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
                "size": len(manifest_path.read_bytes()),
            },
        ],
        "package_sha256": package_sha256,
        "packaging_source_revision": PACKAGING_SOURCE,
        "release_key_sha256": RELEASE_KEY,
        "schema_version": 1,
    }
    manifest = json.loads(manifest_path.read_text())
    assert manifest == {
        "authority_sha256": AUTHORITY,
        "kind": "agent-upgrade-repair",
        "node_id": NODE_ID,
        "package": {
            "architecture": "linux-arm64",
            "package_bytes": len(raw),
            "package_sha256": package_sha256,
            "package_signature": SIGNATURE,
            "package_url": f"https://install.vonkforge.ai/{prefix}/vonk-forge-agent.deb",
            "package_version": VERSION,
            "schema_version": 1,
            "target_binary_digest": TARGET_BINARY,
            "target_build_digest": TARGET_BUILD,
        },
        "schema_version": 1,
    }
    assert manifest_path.read_bytes() == _canonical(manifest)
    assert not any(
        part in {"current", "latest", "dev", "stable", "apt", "artifacts"}
        for entry in plan["objects"]
        for part in entry["key"].split("/")
    )


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"package_bytes": 1}, "inconsistent"),
        ({"repair_node_id": "spk_" + "f" * 32}, "inconsistent"),
        ({"repair_authority_sha256": "f" * 64}, "inconsistent"),
        ({"binary_source_revision": PACKAGING_SOURCE}, "inconsistent"),
        ({"package_signature": "f" * 127}, "inconsistent"),
    ],
)
def test_assemble_rejects_inconsistent_verifier_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    override: dict[str, object],
    message: str,
) -> None:
    raw = b"repair\n"
    deb = tmp_path / "repair.deb"
    deb.write_bytes(raw)
    deb.chmod(0o644)
    monkeypatch.setattr(
        PUBLICATION,
        "_invoke_verifier",
        lambda *_arguments: _verified(raw, **override),
    )
    with pytest.raises(PUBLICATION.PublicationError, match=message):
        PUBLICATION.assemble(
            argparse.Namespace(
                deb=deb,
                expected_node_id=NODE_ID,
                expected_authority_sha256=AUTHORITY,
                output=tmp_path / "bundle",
                **_authority_args(),
            )
        )


@pytest.mark.parametrize(
    "authority_override",
    (
        {"expected_release_key_sha256": "0" * 64},
        {
            "expected_binary_source_revision": "a122909feaa3" + "2" * 28,
        },
        {"expected_packaging_source_revision": "3" * 40},
    ),
)
def test_assemble_rejects_wrong_external_signer_or_source_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    authority_override: dict[str, str],
) -> None:
    raw = b"self-consistent but externally unauthorized repair\n"
    deb = tmp_path / "repair.deb"
    deb.write_bytes(raw)
    deb.chmod(0o644)
    monkeypatch.setattr(
        PUBLICATION,
        "_invoke_verifier",
        lambda *_arguments: _verified(raw),
    )
    authority = {**_authority_args(), **authority_override}
    with pytest.raises(PUBLICATION.PublicationError, match="inconsistent"):
        PUBLICATION.assemble(
            argparse.Namespace(
                deb=deb,
                expected_node_id=NODE_ID,
                expected_authority_sha256=AUTHORITY,
                output=tmp_path / "bundle",
                **authority,
            )
        )


def test_publish_is_deb_first_manifest_last_and_exact_replay_is_allowed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle, raw = _assemble(tmp_path, monkeypatch)
    destination = tmp_path / "published"
    writes: list[str] = []
    original = PUBLICATION.FilesystemStore.write

    def recording_write(store: object, key: str, content: bytes) -> None:
        writes.append(key)
        original(store, key, content)

    monkeypatch.setattr(PUBLICATION.FilesystemStore, "write", recording_write)
    arguments = argparse.Namespace(
        bundle=bundle,
        filesystem=destination,
        rclone_remote=None,
        **_authority_args(),
    )
    PUBLICATION.publish(arguments)
    assert [Path(key).name for key in writes] == [
        "vonk-forge-agent.deb",
        "manifest.json",
    ]
    plan, _, _ = _bundle_files(bundle)
    assert (destination / plan["objects"][0]["key"]).read_bytes() == raw

    writes.clear()
    PUBLICATION.publish(arguments)
    assert writes == []


def test_publish_preflights_conflicts_before_any_remote_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle, _ = _assemble(tmp_path, monkeypatch)
    plan, _, _ = _bundle_files(bundle)
    destination = tmp_path / "published"
    manifest = destination / plan["objects"][1]["key"]
    manifest.parent.mkdir(parents=True)
    manifest.write_bytes(b"different immutable manifest\n")
    manifest.chmod(0o644)

    with pytest.raises(PUBLICATION.PublicationError, match="overwrite immutable"):
        PUBLICATION.publish(
            argparse.Namespace(
                bundle=bundle,
                filesystem=destination,
                rclone_remote=None,
                **_authority_args(),
            )
        )
    assert not (destination / plan["objects"][0]["key"]).exists()
    assert manifest.read_bytes() == b"different immutable manifest\n"


def test_filesystem_publication_never_overwrites_a_concurrent_creator(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = PUBLICATION.FilesystemStore(tmp_path / "published")
    key = "repair-capsules/node/authority/package/vonk-forge-agent.deb"
    original_link = PUBLICATION.os.link

    def race(source: Path, destination: Path, **kwargs: object) -> None:
        destination.write_bytes(b"concurrent immutable bytes\n")
        destination.chmod(0o644)
        original_link(source, destination, **kwargs)

    monkeypatch.setattr(PUBLICATION.os, "link", race)
    with pytest.raises(PUBLICATION.PublicationError, match="overwrite immutable"):
        store.write(key, b"publisher bytes\n")
    assert (tmp_path / "published" / key).read_bytes() == (
        b"concurrent immutable bytes\n"
    )


def test_publish_rejects_manifest_without_package_and_unknown_objects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle, _ = _assemble(tmp_path, monkeypatch)
    plan, _, manifest_source = _bundle_files(bundle)
    destination = tmp_path / "published"
    manifest = destination / plan["objects"][1]["key"]
    manifest.parent.mkdir(parents=True)
    manifest.write_bytes(manifest_source.read_bytes())
    manifest.chmod(0o644)
    with pytest.raises(PUBLICATION.PublicationError, match="without its package"):
        PUBLICATION.publish(
            argparse.Namespace(
                bundle=bundle,
                filesystem=destination,
                rclone_remote=None,
                **_authority_args(),
            )
        )

    manifest.unlink()
    unknown = manifest.parent / "latest"
    unknown.write_bytes(b"forbidden alias\n")
    unknown.chmod(0o644)
    with pytest.raises(PUBLICATION.PublicationError, match="unknown object"):
        PUBLICATION.publish(
            argparse.Namespace(
                bundle=bundle,
                filesystem=destination,
                rclone_remote=None,
                **_authority_args(),
            )
        )


def test_publish_reverifies_package_and_rejects_manifest_tampering(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle, raw = _assemble(tmp_path, monkeypatch)
    _, _, manifest_path = _bundle_files(bundle)
    manifest = json.loads(manifest_path.read_text())
    manifest["package"]["package_signature"] = "f" * 128
    manifest_path.write_bytes(_canonical(manifest))
    manifest_path.chmod(0o644)
    plan = json.loads((bundle / "publication-plan.json").read_text())
    plan["objects"][1]["sha256"] = hashlib.sha256(
        manifest_path.read_bytes()
    ).hexdigest()
    plan["objects"][1]["size"] = len(manifest_path.read_bytes())
    (bundle / "publication-plan.json").write_bytes(_canonical(plan))
    (bundle / "publication-plan.json").chmod(0o644)
    monkeypatch.setattr(
        PUBLICATION,
        "_invoke_verifier",
        lambda *_arguments: _verified(raw),
    )

    with pytest.raises(
        PUBLICATION.PublicationError, match="does not match verified package"
    ):
        PUBLICATION.publish(
            argparse.Namespace(
                bundle=bundle,
                filesystem=tmp_path / "published",
                rclone_remote=None,
                **_authority_args(),
            )
        )


def test_publish_rejects_traversal_and_symlink_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle, _ = _assemble(tmp_path, monkeypatch)
    plan_path = bundle / "publication-plan.json"
    plan = json.loads(plan_path.read_text())
    plan["objects"][0]["key"] = "repair-capsules/../vonk-forge-agent.deb"
    plan_path.write_bytes(_canonical(plan))
    plan_path.chmod(0o644)
    with pytest.raises(PUBLICATION.PublicationError, match="object key"):
        PUBLICATION.publish(
            argparse.Namespace(
                bundle=bundle,
                filesystem=tmp_path / "published",
                rclone_remote=None,
                **_authority_args(),
            )
        )

    destination = tmp_path / "destination"
    destination.symlink_to(tmp_path / "elsewhere", target_is_directory=True)
    with pytest.raises(PUBLICATION.PublicationError, match="unsafe"):
        PUBLICATION.FilesystemStore(destination)


def test_rclone_publication_verifies_package_before_manifest_and_public_origin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle, raw = _assemble(tmp_path, monkeypatch)
    plan, _, _ = _bundle_files(bundle)
    objects = {
        entry["key"]: (bundle / "objects" / entry["key"]).read_bytes()
        for entry in plan["objects"]
    }
    events: list[str] = []

    class FakeStore:
        def __init__(self, _remote: str):
            self.values: dict[str, bytes] = {}

        def list(self, _prefix: str) -> set[str]:
            return set()

        def read(self, key: str, _maximum: int) -> bytes | None:
            events.append(f"read:{Path(key).name}")
            return self.values.get(key)

        def write(self, key: str, content: bytes) -> None:
            events.append(f"write:{Path(key).name}")
            self.values[key] = content

    monkeypatch.setattr(PUBLICATION, "RcloneStore", FakeStore)

    def public_bytes(key: str, _maximum: int) -> bytes:
        events.append(f"public:{Path(key).name}")
        return objects[key]

    monkeypatch.setattr(PUBLICATION, "_public_bytes", public_bytes)
    monkeypatch.setattr(
        PUBLICATION,
        "_invoke_verifier",
        lambda *_arguments: _verified(raw),
    )
    PUBLICATION.publish(
        argparse.Namespace(
            bundle=bundle,
            filesystem=None,
            rclone_remote="r2:install",
            **_authority_args(),
        )
    )
    assert events.index("write:vonk-forge-agent.deb") < events.index(
        "write:manifest.json"
    )
    assert events.index(
        "read:vonk-forge-agent.deb", events.index("write:vonk-forge-agent.deb")
    ) < events.index("write:manifest.json")
    assert events[-2:] == ["public:vonk-forge-agent.deb", "public:manifest.json"]


def test_listed_rclone_stat_failure_is_fail_closed_before_any_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle, raw = _assemble(tmp_path, monkeypatch)
    monkeypatch.setattr(
        PUBLICATION,
        "_invoke_verifier",
        lambda *_arguments: _verified(raw),
    )
    calls: list[list[str]] = []

    def fake_run(
        arguments: list[str], **_kwargs: object
    ) -> subprocess.CompletedProcess:
        calls.append(arguments)
        if arguments[1] == "lsf":
            return subprocess.CompletedProcess(
                arguments, 0, stdout="manifest.json\n", stderr=""
            )
        if arguments[1] == "lsjson":
            return subprocess.CompletedProcess(
                arguments, 7, stdout="", stderr="transient auth failure"
            )
        raise AssertionError(
            f"unexpected write or read after stat failure: {arguments}"
        )

    monkeypatch.setattr(PUBLICATION.subprocess, "run", fake_run)
    with pytest.raises(PUBLICATION.PublicationError, match="metadata lookup failed"):
        PUBLICATION.publish(
            argparse.Namespace(
                bundle=bundle,
                filesystem=None,
                rclone_remote="r2:install",
                **_authority_args(),
            )
        )
    assert all(arguments[1] != "copyto" for arguments in calls)


def test_rclone_operations_have_bounded_metadata_and_transfer_timeouts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    values: dict[str, bytes] = {}
    observed: list[tuple[str, int | None]] = []

    def fake_run(arguments: list[str], **kwargs: object) -> subprocess.CompletedProcess:
        operation = arguments[1]
        observed.append((operation, kwargs.get("timeout")))
        target = arguments[-1] if operation == "copyto" else arguments[2]
        if operation == "lsf":
            return subprocess.CompletedProcess(arguments, 0, stdout="", stderr="")
        if operation == "copyto":
            values[target] = Path(arguments[-2]).read_bytes()
            return subprocess.CompletedProcess(arguments, 0)
        if operation == "lsjson":
            return subprocess.CompletedProcess(
                arguments,
                0,
                stdout=json.dumps({"IsDir": False, "Size": len(values[target])}),
                stderr="",
            )
        if operation == "cat":
            return subprocess.CompletedProcess(arguments, 0, stdout=values[target])
        raise AssertionError(arguments)

    monkeypatch.setattr(PUBLICATION.subprocess, "run", fake_run)
    store = PUBLICATION.RcloneStore("r2:install")
    assert store.list("repair-capsules/node/authority/package") == set()
    key = "repair-capsules/node/authority/package/vonk-forge-agent.deb"
    store.write(key, b"repair bytes\n")
    assert store.read(key, 1024) == b"repair bytes\n"
    assert observed == [
        ("lsf", PUBLICATION.RCLONE_METADATA_TIMEOUT),
        ("copyto", PUBLICATION.RCLONE_TRANSFER_TIMEOUT),
        ("lsjson", PUBLICATION.RCLONE_METADATA_TIMEOUT),
        ("cat", PUBLICATION.RCLONE_TRANSFER_TIMEOUT),
    ]


def test_public_verification_installs_a_strict_no_redirect_handler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handlers: list[object] = []

    class Response:
        status = 200

        def __enter__(self) -> Self:
            return self

        def __exit__(self, *_arguments: object) -> None:
            return None

        def geturl(self) -> str:
            return "https://install.vonkforge.ai/exact"

        def read(self, _maximum: int) -> bytes:
            return b"exact bytes\n"

    class Opener:
        def open(self, _request: object, timeout: int) -> Response:
            assert timeout == 20
            return Response()

    def build_opener(*selected: object) -> Opener:
        handlers.extend(selected)
        return Opener()

    monkeypatch.setattr(PUBLICATION.urllib.request, "build_opener", build_opener)
    assert PUBLICATION._public_bytes("exact", 1024) == b"exact bytes\n"
    assert len(handlers) == 1
    assert isinstance(handlers[0], PUBLICATION._NoRedirect)
    assert handlers[0].redirect_request(None, None, 302, "", None, "elsewhere") is None


def test_apt_metadata_rejects_node_bound_repair_version() -> None:
    for channel in ("dev", "stable"):
        result = subprocess.run(
            [ROOT / "scripts/agent-apt-metadata", channel, VERSION],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 64
        assert result.stdout == ""
        assert result.stderr == "agent apt metadata is invalid\n"
