from __future__ import annotations

import os
import stat
import subprocess
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
from cryptography.x509.oid import NameOID

from vonk_control import runtime_init
from vonk_control.gateway.keys import MASTER_KEY_FILE
from vonk_control.pki.controller_leaf import LeafPair, mint_leaf
from vonk_control.runtime_init import (
    SharedRuntimePaths,
    prepare_shared_volumes,
    stage_compose_secrets,
    stage_controller_tls,
    stage_private_key,
    stage_runtime_assets,
    stage_runtime_file,
    write_runtime_asset_inventory,
)


def test_runtime_secret_can_be_staged_without_host_owner_assumptions(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.pem"
    source.write_bytes(b"private-runtime-key\n")
    source.chmod(0o660)
    destination = tmp_path / "normalized" / "runtime-private-key.pem"

    stage_private_key(
        source,
        destination,
        owner_uid=os.geteuid(),
        owner_gid=os.getegid(),
    )

    assert destination.read_bytes() == b"private-runtime-key\n"
    assert destination.stat().st_mode & 0o777 == 0o444


def test_staged_api_private_key_is_owned_by_the_api_and_private(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.pem"
    source.write_bytes(b"private-api-key\n")
    destination = tmp_path / "normalized" / "api-key"

    stage_private_key(
        source,
        destination,
        owner_uid=os.geteuid(),
        owner_gid=os.getegid(),
        mode=0o400,
    )

    assert destination.stat().st_mode & 0o777 == 0o400


def test_public_runtime_file_can_use_the_larger_bounded_asset_limit(
    tmp_path: Path,
) -> None:
    source = tmp_path / "dashboard.json"
    source.write_bytes(b"x" * (16 * 1024 + 1))
    destination = tmp_path / "normalized" / "dashboard.json"

    stage_runtime_file(
        source,
        destination,
        owner_uid=os.geteuid(),
        owner_gid=os.getegid(),
        mode=0o400,
    )

    assert destination.stat().st_size == 16 * 1024 + 1


def test_optional_huggingface_secret_is_normalized_only_when_present(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "normalized"
    source.mkdir()
    token = source / "hf-token"
    token.write_text("hf_test_secret\n")

    runtime_init._stage_optional_private_key(
        source / "hf-token",
        destination / "hf-token",
        owner_uid=os.geteuid(),
        owner_gid=os.getegid(),
    )

    projected = destination / "hf-token"
    assert projected.read_bytes() == b"hf_test_secret\n"
    assert projected.stat().st_mode & 0o777 == 0o400

    token.unlink()
    runtime_init._stage_optional_private_key(
        source / "hf-token", destination / "hf-token"
    )
    assert not projected.exists()


def test_optional_litellm_upstream_key_is_not_projected_when_blank(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source" / "litellm-upstream-key"
    source.parent.mkdir()
    source.write_text("")
    destination = tmp_path / "normalized" / "litellm-upstream-key"

    runtime_init._stage_optional_private_key(source, destination)

    assert not destination.exists()


def test_optional_huggingface_secret_treats_dev_null_as_absent(tmp_path: Path) -> None:
    if not runtime_init._is_null_device(Path("/dev/null")):
        pytest.skip("host null-device identity is not the Linux /dev/null device")
    destination = tmp_path / "normalized" / "hf-token"
    destination.parent.mkdir(parents=True)
    destination.write_text("stale token")

    runtime_init._stage_optional_private_key(Path("/dev/null"), destination)

    assert not destination.exists()


@pytest.mark.built_image
@pytest.mark.lane  # Uses the shipped runtime and its canonical protocol dependency.
def test_optional_huggingface_secret_handles_bind_mounted_dev_null_in_container(
    tmp_path: Path,
    controller_image: str,
) -> None:
    destination = tmp_path / "normalized" / "hf-token"
    command = (
        "from pathlib import Path; "
        "from vonk_control.runtime_init import _stage_optional_private_key; "
        "destination = Path('/tmp/normalized/hf-token'); destination.parent.mkdir(parents=True, exist_ok=True); "
        "destination.write_text('stale token'); "
        "_stage_optional_private_key(Path('/run/secrets/hf-token'), destination); "
        "assert not destination.exists()"
    )
    subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            # As runtime-init runs in Compose; the image default is 10001.
            "--user",
            "0:0",
            "--entrypoint",
            "python",
            "-v",
            "/dev/null:/run/secrets/hf-token:ro",
            "-v",
            f"{tmp_path}:/tmp/normalized",
            controller_image,
            "-c",
            command,
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    assert not destination.exists()


def test_runtime_assets_follow_the_shipped_release_exactly(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Ownership is root's in the container; the test process may not chown.
    monkeypatch.setattr(os, "fchown", lambda *_args: None)
    source = tmp_path / "image"
    (source / "prometheus").mkdir(parents=True)
    (source / "caddy").mkdir()
    (source / "caddy/Caddyfile").write_text("new relay\n")
    (source / "prometheus/prometheus.yml").write_text("{}\n")
    destination = tmp_path / "volume"
    (destination / "caddy").mkdir(parents=True)
    (destination / "caddy/Caddyfile").write_text("old relay\n")
    (destination / "retired").mkdir()
    (destination / "retired/old.yml").write_text("gone\n")

    runtime_init.write_runtime_asset_inventory(source)
    stage_runtime_assets(source, destination)

    staged = {
        path.relative_to(destination).as_posix(): path
        for path in destination.rglob("*")
        if path.is_file()
    }
    assert set(staged) == {"caddy/Caddyfile", "prometheus/prometheus.yml"}
    assert staged["caddy/Caddyfile"].read_text() == "new relay\n"
    assert all(stat.S_IMODE(path.stat().st_mode) == 0o444 for path in staged.values())
    assert not (destination / "retired").exists()


def test_shared_volume_preparation_preserves_each_consumer_boundary(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    roots = {
        name: tmp_path / name.replace("_", "-")
        for name in (
            "agent_artifacts",
            "model_cache",
            "routes",
            "supervisor",
            "state",
            "gateway",
        )
    }
    ownership: list[tuple[int, int]] = []
    monkeypatch.setattr(
        os,
        "fchown",
        lambda _descriptor, uid, gid: ownership.append((uid, gid)),
    )

    prepare_shared_volumes(SharedRuntimePaths(**roots))

    assert ownership == [
        (10001, 10001),
        (10001, 10001),
        (10001, 10001),
        (10001, 10001),
        (10001, 10001),
        (10002, 10001),
        (-1, 10001),
    ]
    expected_paths = (
        roots["state"],
        roots["agent_artifacts"],
        roots["model_cache"],
        roots["routes"],
        roots["routes"] / "generations",
        roots["supervisor"],
        roots["gateway"],
    )
    assert {
        path.relative_to(tmp_path).as_posix(): path.stat().st_mode & 0o777
        for path in expected_paths
    } == {
        "state": 0o750,
        "agent-artifacts": 0o750,
        "model-cache": 0o750,
        "routes": 0o750,
        "routes/generations": 0o750,
        "supervisor": 0o750,
        "gateway": 0o770,
    }


def test_shared_volume_preparation_rejects_symlinked_component(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(os, "fchown", lambda *_args: None)
    outside = tmp_path / "outside"
    outside.mkdir()
    routes = tmp_path / "routes"
    routes.symlink_to(outside, target_is_directory=True)
    paths = SharedRuntimePaths(
        agent_artifacts=tmp_path / "agent-artifacts",
        model_cache=tmp_path / "model-cache",
        routes=routes,
        supervisor=tmp_path / "supervisor",
        state=tmp_path / "state",
        gateway=tmp_path / "gateway",
    )

    with pytest.raises(Exception):  # noqa: B017 -- observable effects and recovery establish the rejection
        prepare_shared_volumes(paths)

    assert list(outside.iterdir()) == []
    routes.unlink()
    prepare_shared_volumes(paths)
    assert routes.is_dir()
    assert not routes.is_symlink()
    assert list(outside.iterdir()) == []


def test_gateway_master_key_is_staged_only_outside_worker_volume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Catches copying API gateway authority into the worker's normalized mount.
    source, shared = tmp_path / "source", tmp_path / "normalized"
    source.mkdir()
    shared.mkdir()
    master = source / "litellm-master-key"
    master.write_bytes(b"test-master-key")
    stale = shared / "gateway-litellm-master-key"
    stale.write_bytes(b"previous-shared-key")
    gateway_root = tmp_path / "gateway"
    gateway_root.mkdir()
    api_key = gateway_root / runtime_init.GATEWAY_MASTER_KEY_PATH.relative_to(
        "/gateway-secrets"
    )
    # This host test cannot assign the container's group. The Linux process
    # acceptance exercises the real uid/gid boundary.
    monkeypatch.setattr(os, "fchown", lambda *_args: None)
    chown = os.chown

    def preserve_host_owner(path, *args, **kwargs):
        assert path != gateway_root, "staging must preserve the gateway bind owner"
        return chown(path, *args, **kwargs)

    monkeypatch.setattr(os, "chown", preserve_host_owner)
    staged: list[tuple[Path, int, int]] = []
    stage = runtime_init.stage_private_key

    def record(source, destination, **kwargs):
        if source == master:
            staged.append((destination, kwargs["owner_uid"], kwargs["mode"]))
            return stage(
                source,
                destination,
                owner_uid=os.geteuid(),
                owner_gid=os.getegid(),
                mode=kwargs["mode"],
                preserve_parent=kwargs.get("preserve_parent", False),
            )
        return destination

    monkeypatch.setattr(runtime_init, "stage_private_key", record)
    monkeypatch.setattr(
        runtime_init, "_stage_optional_private_key", lambda *_a, **_k: None
    )
    stage_compose_secrets(source, shared, gateway_master_key=api_key)
    assert api_key.read_bytes() == master.read_bytes()
    assert api_key.stat().st_uid == gateway_root.stat().st_uid
    assert stat.S_IMODE(api_key.stat().st_mode) == 0o640
    assert stat.S_IMODE(api_key.parent.stat().st_mode) == 0o770
    assert api_key.parent.stat().st_uid == gateway_root.stat().st_uid
    assert not stale.exists()
    assert staged == [
        (api_key, gateway_root.stat().st_uid, 0o640),
        (shared / "litellm-master-key", 10002, 0o400),
    ]
    api_key.parent.chmod(0o755)
    stage_compose_secrets(source, shared, gateway_master_key=api_key)
    assert stat.S_IMODE(api_key.parent.stat().st_mode) == 0o770
    stale.write_bytes(b"previous-shared-key")
    with monkeypatch.context() as patch:

        def unavailable(source, destination, **kwargs):
            if destination == api_key:
                raise OSError("authority publication unavailable")
            return record(source, destination, **kwargs)

        patch.setattr(runtime_init, "stage_private_key", unavailable)
        with pytest.raises(Exception):  # noqa: B017 -- check publication and retirement effects
            stage_compose_secrets(source, shared, gateway_master_key=api_key)
    assert stale.read_bytes() == b"previous-shared-key"
    assert api_key.read_bytes() == master.read_bytes()
    stage_compose_secrets(source, shared, gateway_master_key=api_key)
    assert not stale.exists()

    assert MASTER_KEY_FILE == runtime_init.GATEWAY_MASTER_KEY_PATH


@pytest.mark.parametrize("supplied", [True, False])
def test_admin_password_is_staged_for_the_api_when_compose_supplies_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, supplied: bool
) -> None:
    # Catches the non-root API losing its administrator password, or an older
    # Compose file (no such secret for runtime-init) failing the whole run.
    source, shared, gateway = tmp_path / "source", tmp_path / "shared", tmp_path / "gw"
    for directory in (source, shared, gateway):
        directory.mkdir()
    for name in (
        "host-runtime-grant-private-key",
        "database-url",
        "token-signing-key",
        "metrics-token",
        "agent-client-ca",
        "agent-intermediate-certificate",
        "controller-ca",
        "agent-proxy-auth",
        "litellm-master-key",
        "litellm-database-url",
    ):
        (source / name).write_bytes(b"secret\n")
    if supplied:
        (source / "admin-password").write_bytes(b"administrator password\n")
    # This host test cannot assign the consumers' owners; the built-image
    # test exercises the real uid/gid boundary.
    monkeypatch.setattr(os, "fchown", lambda *_args: None)
    order: list[str] = []
    stage = runtime_init.stage_private_key

    def observe(source_path, destination, **kwargs):
        order.append(destination.name)
        return stage(source_path, destination, **kwargs)

    monkeypatch.setattr(runtime_init, "stage_private_key", observe)

    stage_compose_secrets(
        source, shared, gateway_master_key=gateway / "authority" / "master-key"
    )

    staged = shared / "admin-password"
    if supplied:
        assert staged.read_bytes() == b"administrator password\n"
        assert stat.S_IMODE(staged.stat().st_mode) == 0o400
        # First: a failure leaves a fresh install without its database URL too.
        assert order.index("admin-password") < order.index("database-url")
    else:
        assert not staged.exists()
    assert (shared / "database-url").read_bytes() == b"secret\n"


def test_existing_object_store_directories_stop_new_files_being_copy_on_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A store created before the attribute was set keeps its old directories,
    # and a directory only passes the attribute to files created after it.
    root = tmp_path / "model-cache"
    (root / "objects" / "ab").mkdir(parents=True)
    (root / "partials" / "set-one").mkdir(parents=True)
    (root / "partials" / "set-one" / "x.part").write_bytes(b"x")
    (root / "outside-link").symlink_to(tmp_path)
    marked: list[str] = []
    monkeypatch.setattr(
        runtime_init,
        "_disable_copy_on_write",
        lambda directory: marked.append(directory.relative_to(root).as_posix()),
    )

    runtime_init._disable_copy_on_write_below(root)

    assert sorted(marked) == ["objects", "partials", "partials/set-one"]


@pytest.mark.parametrize("fault", ["missing-inventory", "replace-unavailable"])
def test_public_runtime_assets_end_unknown_preserve_previous_and_allow_fresh_staging(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fault: str,
) -> None:
    monkeypatch.setattr(os, "fchown", lambda *_args: None)
    source = tmp_path / "image"
    source.mkdir()
    destination = tmp_path / "volume"
    destination.mkdir()
    previous = destination / "config"
    previous.write_text("verified previous")
    replace = os.replace
    calls = []

    def unavailable(*args):
        calls.append(args)
        raise OSError("storage temporarily unavailable")

    if fault == "replace-unavailable":
        (source / "config").write_text("current kit")
        runtime_init.write_runtime_asset_inventory(source)
        monkeypatch.setattr(os, "replace", unavailable)
    ended = False
    try:
        stage_runtime_assets(source, destination)
    except Exception:  # noqa: BLE001 - observe bounded ending without asserting taxonomy
        ended = True
    assert ended
    assert previous.read_text() == "verified previous"
    assert len(calls) == (3 if fault == "replace-unavailable" else 0)
    monkeypatch.setattr(os, "replace", replace)
    (source / "config").write_text("current kit")
    runtime_init.write_runtime_asset_inventory(source)
    stage_runtime_assets(source, destination)
    assert previous.read_text() == "current kit"
    assert not list(destination.glob(".*.new"))


def test_partial_public_inventory_preserves_omitted_member_and_healthy_sibling(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(os, "fchown", lambda *_: None)
    source, destination = tmp_path / "kit", tmp_path / "volume"
    source.mkdir()
    destination.mkdir()
    for name in ("healthy", "vanished"):
        (source / name).write_text("new")
        (destination / name).write_text("previous")
    runtime_init.write_runtime_asset_inventory(source)
    (source / "vanished").unlink()
    ended = False
    try:
        stage_runtime_assets(source, destination)
    except Exception:  # noqa: BLE001 - observe bounded ending without asserting taxonomy
        ended = True
    assert ended
    assert (destination / "healthy").read_text() == "previous"
    assert (destination / "vanished").read_text() == "previous"
    (source / "vanished").write_text("new")
    stage_runtime_assets(source, destination)
    assert (destination / "vanished").read_text() == "new"
    assert (destination / "healthy").read_text() == "new"


@pytest.mark.parametrize("fault", ["read", "parent", "write", "replace", "cleanup"])
def test_direct_private_staging_ends_preserves_and_fresh_request_recovers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fault: str,
) -> None:
    source, destination = tmp_path / "key", tmp_path / "volume" / "key"
    source.write_bytes(b"current verified secret")
    destination.parent.mkdir()
    destination.write_bytes(b"previous secret")
    calls = []
    with monkeypatch.context() as patch:

        def unavailable(*args, **kwargs):
            calls.append(args)
            raise OSError("temporary storage failure")

        if fault == "read":
            patch.setattr(os, "read", unavailable)
        elif fault == "parent":
            patch.setattr(Path, "mkdir", unavailable)
        elif fault == "write":

            def no_progress(*args):
                calls.append(args)
                return 0

            patch.setattr(os, "write", no_progress)
        else:
            patch.setattr(os, "replace", unavailable)
            if fault == "cleanup":
                patch.setattr(Path, "unlink", unavailable)
        ended = False
        try:
            stage_private_key(
                source, destination, owner_uid=os.geteuid(), owner_gid=os.getegid()
            )
        except Exception:  # noqa: BLE001 - observe bounded ending without asserting taxonomy
            ended = True
        assert ended
        assert destination.read_bytes() == b"previous secret"
        assert 3 <= len(calls) <= 6
    stage_private_key(
        source, destination, owner_uid=os.geteuid(), owner_gid=os.getegid()
    )
    assert destination.read_bytes() == b"current verified secret"


def test_kit_inventory_assembly_ends_preserves_and_fresh_assembly_stages(
    tmp_path, monkeypatch
):

    monkeypatch.setattr(os, "fchown", lambda *_args: None)
    kit = tmp_path / "kit"
    kit.mkdir()
    (kit / "one").write_bytes(b"one")
    (kit / "two").write_bytes(b"two")
    assert write_runtime_asset_inventory(kit)
    previous = (kit / ".inventory.json").read_bytes()
    observed = []
    original = Path.lstat

    def unavailable(path, *args, **kwargs):
        if path == kit / "two":
            observed.append(True)
            raise OSError("kit member observation unavailable")
        return original(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "lstat", unavailable)
        assert not write_runtime_asset_inventory(kit)
        assert len(observed) == 3
        assert (kit / ".inventory.json").read_bytes() == previous
    assert write_runtime_asset_inventory(kit)
    destination = tmp_path / "staged"
    stage_runtime_assets(kit, destination)
    assert (destination / "one").read_bytes() == b"one"
    assert (destination / "two").read_bytes() == b"two"


def test_compose_optional_secret_observation_ends_preserves_and_fresh_staging_settles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, destination = tmp_path / "secrets", tmp_path / "normalized"
    source.mkdir()
    destination.mkdir()
    optional = source / "litellm-upstream-key"
    optional.write_bytes(b"")
    projected = destination / "litellm-upstream-key"
    projected.write_bytes(b"previous secret")
    # Other projections are independent; exercise the actual Compose staging
    # owner and optional-secret observation/cleanup boundary.
    monkeypatch.setattr(runtime_init, "stage_private_key", lambda *_a, **_k: None)
    monkeypatch.setattr(os, "fchown", lambda *_args: None)
    original = Path.lstat
    attempts = []
    with monkeypatch.context() as patch:

        def unavailable(path, *args, **kwargs):
            if path == optional:
                attempts.append(True)
                raise PermissionError("optional source observation unavailable")
            return original(path, *args, **kwargs)

        patch.setattr(Path, "lstat", unavailable)
        ended = False
        try:
            stage_compose_secrets(
                source,
                destination,
                gateway_master_key=tmp_path / "gateway" / "master-key",
            )
        except Exception:  # noqa: BLE001 -- bound, preservation and fresh completion
            ended = True
        assert ended
        assert len(attempts) == 3
        assert projected.read_bytes() == b"previous secret"
    stage_compose_secrets(
        source, destination, gateway_master_key=tmp_path / "gateway" / "master-key"
    )
    assert not projected.exists()


def test_runtime_init_marks_only_a_completed_run_healthy_then_idles(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Catches a resident helper that reports healthy unstaged, or busy-loops."""
    monkeypatch.setattr(runtime_init.signal, "signal", lambda *_args: None)
    marker = tmp_path / "runtime-staged"
    idles: list[str] = []

    def unavailable() -> None:
        raise OSError("volume observation unavailable")

    monkeypatch.setattr(runtime_init, "stage_runtime", unavailable)
    with pytest.raises(OSError):
        runtime_init.main(marker=marker, idle=lambda: idles.append("idle"))
    assert not marker.exists()
    assert idles == []

    monkeypatch.setattr(runtime_init, "stage_runtime", lambda: None)
    runtime_init.main(marker=marker, idle=lambda: idles.append("idle"))
    assert marker.read_text() == "staged\n"
    assert idles == ["idle"]


# Controller TLS leaf renewal ------------------------------------------------

_NAMES = ["vonk-forge.local", "enroll.vonk-forge.local", "agents.vonk-forge.local"]
_NOW = datetime.now(UTC)


def _pki(
    secrets: Path, *, password: bytes = b"intermediate-password"
) -> Callable[[list[str], datetime], LeafPair]:
    """Write the installer's CA material; returns a leaf writer for its pair."""
    root_key, key = (
        ed25519.Ed25519PrivateKey.generate(),
        (ed25519.Ed25519PrivateKey.generate()),
    )

    def ca(subject_key, name, issuer, issuer_key, length):
        subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
        ski = x509.SubjectKeyIdentifier.from_public_key(subject_key.public_key())
        return (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(issuer.subject if issuer else subject)
            .public_key(subject_key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(_NOW - timedelta(days=400))
            .not_valid_after(_NOW + timedelta(days=1000))
            .add_extension(ski, False)
            .add_extension(x509.BasicConstraints(ca=True, path_length=length), True)
            .sign(issuer_key, algorithm=None)
        )

    root = ca(root_key, "Root", None, root_key, 1)
    intermediate = ca(key, "Intermediate", root, root_key, 0)
    secrets.mkdir(exist_ok=True)
    (secrets / "agent-intermediate-certificate").write_bytes(
        intermediate.public_bytes(serialization.Encoding.PEM)
    )
    (secrets / "step-ca-intermediate-key").write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.BestAvailableEncryption(password),
        )
    )
    (secrets / "step-ca-password").write_bytes(password + b"\n")

    def installer_leaf(names: list[str], issued: datetime) -> LeafPair:
        pair = mint_leaf(names, intermediate, key, issued)
        for name, content in (
            ("controller-server-certificate", pair.certificate),
            ("controller-server-key", pair.key),
        ):
            (secrets / name).unlink(missing_ok=True)
            (secrets / name).write_bytes(content)
        return pair

    return installer_leaf


def _served(volume: Path) -> tuple[x509.Certificate, bytes]:
    certificate = LeafPair.from_served_file(
        (volume / "controller-server.pem").read_bytes()
    ).certificate
    return x509.load_pem_x509_certificates(certificate)[0], certificate


def _names(leaf: x509.Certificate) -> list[str]:
    return leaf.extensions.get_extension_for_class(
        x509.SubjectAlternativeName
    ).value.get_values_for_type(x509.DNSName)


@pytest.fixture
def tls(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[Path, Path]:
    monkeypatch.setattr(os, "fchown", lambda *_args: None)
    volume = tmp_path / "controller-tls"
    volume.mkdir()
    return tmp_path / "secrets", volume


def test_a_leaf_near_expiry_is_renewed_with_the_installer_names(
    tls: tuple[Path, Path],
) -> None:
    """Catches a redeploy that keeps serving the installer leaf until it expires."""
    secrets, volume = tls
    installer = _pki(secrets)(_NAMES, _NOW - timedelta(days=340))  # 57 days left

    stage_controller_tls(secrets, volume)

    leaf, certificate = _served(volume)
    assert certificate != installer.certificate
    assert leaf.not_valid_after_utc - _NOW > timedelta(days=390)
    assert _names(leaf) == _NAMES
    intermediate = x509.load_pem_x509_certificate(
        (secrets / "agent-intermediate-certificate").read_bytes()
    )
    leaf.verify_directly_issued_by(intermediate)
    assert (
        leaf.extensions.get_extension_for_class(
            x509.AuthorityKeyIdentifier
        ).value.key_identifier
        == intermediate.extensions.get_extension_for_class(
            x509.SubjectKeyIdentifier
        ).value.digest
    )
    served = volume / "controller-server.pem"
    key = LeafPair.from_served_file(served.read_bytes()).key
    assert (
        serialization.load_pem_private_key(key, None).public_key() == leaf.public_key()
    )
    assert stat.S_IMODE(served.stat().st_mode) == 0o440


def test_a_valid_staged_leaf_is_served_again_unchanged(
    tls: tuple[Path, Path],
) -> None:
    """Catches minting a new leaf (and key) on every start."""
    secrets, volume = tls
    _pki(secrets)(_NAMES, _NOW - timedelta(days=340))
    stage_controller_tls(secrets, volume)
    first = _served(volume)[1]

    stage_controller_tls(secrets, volume)

    assert _served(volume)[1] == first


def test_a_damaged_staged_file_is_replaced(
    tls: tuple[Path, Path],
) -> None:
    """Catches a corrupt staged file that Caddy keeps loading and never heals."""
    secrets, volume = tls
    _pki(secrets)(_NAMES, _NOW - timedelta(days=340))
    stage_controller_tls(secrets, volume)
    served = volume / "controller-server.pem"
    served.chmod(0o600)
    served.write_bytes(b"not a certificate chain")

    stage_controller_tls(secrets, volume)

    LeafPair.from_served_file(served.read_bytes())


def test_an_installer_name_change_wins_over_a_newer_staged_leaf(
    tls: tuple[Path, Path],
) -> None:
    """Catches a renewed leaf that masks the installer's new host name."""
    secrets, volume = tls
    installer_leaf = _pki(secrets)
    installer_leaf(_NAMES, _NOW - timedelta(days=340))
    stage_controller_tls(secrets, volume)  # staged: 397 days, old names
    renamed = [*_NAMES, "vonk-forge-litellm.local"]
    installer = installer_leaf(renamed, _NOW - timedelta(days=10))

    stage_controller_tls(secrets, volume)

    leaf, certificate = _served(volume)
    assert _names(leaf) == renamed
    assert certificate == installer.certificate


def test_a_renewal_fault_is_logged_and_staging_still_completes(
    monkeypatch: pytest.MonkeyPatch, tls: tuple[Path, Path], tmp_path: Path
) -> None:
    """Catches a renewal fault that keeps runtime-init (and every reader) unhealthy."""
    secrets, volume = tls
    _pki(secrets)(_NAMES, _NOW - timedelta(days=340))
    (secrets / "step-ca-password").write_bytes(b"wrong\n")
    for step in (
        "stage_compose_secrets",
        "stage_runtime_assets",
        "prepare_shared_volumes",
    ):
        monkeypatch.setattr(runtime_init, step, lambda: None)
    real = runtime_init.stage_controller_tls
    monkeypatch.setattr(
        runtime_init, "stage_controller_tls", lambda: real(secrets, volume)
    )
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr(runtime_init.signal, "signal", lambda *_args: None)
    marker = tmp_path / "runtime-staged"

    runtime_init.main(marker=marker, idle=lambda: None)

    assert marker.read_text() == "staged\n"
    assert not (volume / "controller-server.pem").exists()


def test_an_older_compose_file_without_the_volume_renews_nothing(
    tls: tuple[Path, Path],
) -> None:
    secrets, volume = tls
    _pki(secrets)(_NAMES, _NOW - timedelta(days=340))
    volume.rmdir()

    stage_controller_tls(secrets, volume)

    assert not volume.exists()


def test_a_crash_while_publishing_a_renewal_leaves_a_matching_pair(
    monkeypatch: pytest.MonkeyPatch, tls: tuple[Path, Path]
) -> None:
    """Catches a key published without its leaf: Caddy would refuse the pair
    and restart-loop, taking every agent and browser listener down. The crash
    hits the second rename, the point where a two-file publication splits."""
    secrets, volume = tls
    installer_leaf = _pki(secrets)
    installer_leaf(_NAMES, _NOW - timedelta(days=10))
    stage_controller_tls(secrets, volume)  # the installer's pair is staged
    installer_leaf(_NAMES, _NOW - timedelta(days=340))  # due: a renewal is minted
    real_replace = Path.replace
    renames: list[Path] = []

    def crash_on_second(self: Path, target: Path) -> Path:
        renames.append(self)
        if len(renames) == 2:
            raise OSError("power lost during publication")
        return real_replace(self, target)

    monkeypatch.setattr(Path, "replace", crash_on_second)
    stage_controller_tls(secrets, volume)

    pair = LeafPair.from_served_file((volume / "controller-server.pem").read_bytes())
    leaf = x509.load_pem_x509_certificates(pair.certificate)[0]
    assert (
        serialization.load_pem_private_key(pair.key, None).public_key()
        == leaf.public_key()
    )
