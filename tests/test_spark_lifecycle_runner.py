from __future__ import annotations

import base64
import importlib.util
import json
import os
import subprocess
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
from typing import TypedDict

import pytest
from vonk_agent_protocol import LifecycleState

ENTRY_POINT = Path(__file__).parent / "acceptance/test_spark_lifecycle.py"


class _Observed(TypedDict):
    # Filled in by the `interactive` callback, which the code under test invokes
    # before the test reads any key, so a read never observes a placeholder.
    command: list[str]
    responses: list[tuple[str, str]]
    forbidden_values: list[str]
    environment: dict[str, str]


def _module():
    specification = importlib.util.spec_from_file_location(
        "spark_lifecycle_runner", ENTRY_POINT
    )
    assert specification is not None and specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


def _failed_profile_application(reason: str) -> dict[str, object]:
    return {
        "id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        "request_key": "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee",
        "profile_id": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
        "profile_digest": "c" * 64,
        "plan_digest": "d" * 64,
        "attempt": 1,
        "retry_of_application_id": None,
        "state": "failed",
        "current_step": 0,
        "total_steps": 1,
        "current_operation_id": None,
        "status_reason": reason,
        "progress": {
            "attempt": 1,
            "completed_steps": 0,
            "total_steps": 1,
            "current_label": "container-build",
            "operation_kind": "fleet-profile.apply",
            "step_results": {},
        },
        "result": None,
        "created_at": "2026-09-10T10:00:00Z",
        "updated_at": "2026-09-10T10:00:01Z",
    }


def test_literal_spark_bootstrap_keeps_pairing_token_only_in_tty_answers(
    tmp_path: Path,
) -> None:
    lifecycle = _module()
    observed: _Observed = {
        "command": [],
        "responses": [],
        "forbidden_values": [],
        "environment": {},
    }

    def interactive(command, **kwargs):
        observed.update(command=command, **kwargs)
        return "installed"

    token = "single-use-pairing-secret"
    environment = {
        "PATH": "/usr/bin:/bin",
        "VONK_INSTALL_BASE_URL": "https://install.example/artifacts/release",
        "VONK_INSTALL_RELEASE_MANIFEST": "/objects/release.json",
        "VONK_INSTALL_RELEASE_SIGNATURE": "/objects/release.sig",
    }
    lifecycle._run_spark_bootstrap(
        "https://install.example/artifacts/release/bootstraps/spark",
        cwd=tmp_path,
        environment=environment,
        enrollment_url="https://enroll.vonk-forge-spark-local.spark.acceptance.invalid:8443",
        ca_sha256="a" * 64,
        pairing_token=token,
        interactive=interactive,
    )

    command = observed["command"]
    assert command[:2] == ["/bin/sh", "-c"]
    assert "curl --fail --location --silent --show-error" in command[2]
    assert "--retry 30 --retry-all-errors" in command[2]
    assert "| sh" not in command[2]
    assert command[3:] == [
        "vonk-bootstrap",
        "https://install.example/artifacts/release/bootstraps/spark",
        "--enroll",
    ]
    answers = [answer for _, answer in observed["responses"]]
    assert (
        "https://enroll.vonk-forge-spark-local.spark.acceptance.invalid:8443" in answers
    )
    assert "a" * 64 in answers
    assert answers.count(token) == 1
    assert observed["forbidden_values"] == [token]
    assert token not in repr(observed["command"])
    assert token not in repr(observed["environment"])


def test_acceptance_controller_configuration_preserves_fixed_ca_and_generation(
    tmp_path: Path,
) -> None:
    lifecycle = _module()
    bundle = tmp_path / "bundle"
    (bundle / "secrets/step-ca").mkdir(parents=True)
    (bundle / "secrets/step-ca/ca.json").write_text(
        json.dumps(
            {
                "authority": {
                    "provisioners": [
                        {
                            "name": "vonk-forge-agent",
                            "claims": {
                                "minTLSCertDuration": "720h",
                                "maxTLSCertDuration": "720h",
                                "defaultTLSCertDuration": "720h",
                                "disableRenewal": True,
                                "disableSmallstepExtensions": True,
                            },
                        }
                    ]
                }
            }
        )
    )
    (bundle / "docker-compose.yaml").write_text(
        "services:\n  control-api:\n    image: ghcr.io/vonk/api@sha256:"
        + "a" * 64
        + "\n    environment:\n      VONK_DEPLOYMENT_MODE: production\n"
        + "  caddy:\n    image: caddy:acceptance\n    networks: [ingress]\n    ports:\n"
        + "      - target: 8443\n        published: 8443\n"
        + "    volumes:\n      - caddy-data:/data\n"
        + "networks:\n  ingress: {}\n  cluster-egress: {}\n"
    )

    import yaml

    compose_path = bundle / "docker-compose.yaml"
    compose_document = yaml.safe_load(compose_path.read_text())
    published = yaml.safe_load(
        (ENTRY_POINT.parents[2] / "deploy/compose/compose.yaml").read_text()
    )
    compose_document["services"]["caddy"]["entrypoint"] = published["services"][
        "caddy"
    ]["entrypoint"]
    compose_path.write_text(yaml.safe_dump(compose_document))

    ca_before = (bundle / "secrets/step-ca/ca.json").read_bytes()
    lifecycle._configure_acceptance_renewal(
        bundle,
        lifetime_seconds=lifecycle.CERTIFICATE_LIFETIME_SECONDS,
        agent_source_address="172.31.42.1",
    )

    assert (bundle / "secrets/step-ca/ca.json").read_bytes() == ca_before
    # Execute the actual shell wrapper and native secret-validating entrypoint.
    # Only the staged mount paths and terminal Caddy executable are fixture
    # resources; no container, network, or readiness result is mocked.
    compose_document = yaml.safe_load(compose_path.read_text())
    service = compose_document["services"]["caddy"]
    secrets = tmp_path / "run/secrets"
    secrets.mkdir(parents=True)
    for name in (
        "controller-server-certificate",
        "controller-server-key",
        "agent-client-ca",
    ):
        (secrets / name).write_text("fixture public certificate material")
    (secrets / "agent-proxy-auth").write_text("a" * 32)
    native_source = (
        ENTRY_POINT.parents[2] / "deploy/compose/caddy/entrypoint.sh"
    ).read_text()
    native = tmp_path / "runtime/caddy/entrypoint.sh"
    native.parent.mkdir(parents=True)
    native.write_text(native_source.replace("/run/secrets/", str(secrets) + "/"))
    executable = tmp_path / "bin/caddy"
    executable.parent.mkdir()
    captured = tmp_path / "caddy-arguments.json"
    executable.write_text('#!/bin/sh\nprintf \'%s\\n\' "$@" > "$CAPTURE_ARGUMENTS"\n')
    executable.chmod(0o755)
    wrapper = [
        argument.replace("$$", "$").replace(
            "/run/vonk-runtime-assets/caddy/entrypoint.sh", str(native)
        )
        for argument in service["entrypoint"]
    ]
    # The former published wrapper discarded command arguments. Its native
    # entrypoint therefore selected the packaged configuration, not the
    # fixture's source-bound configuration. Exercise that exact countercase.
    old_wrapper = wrapper[:3]
    old_wrapper[2] = old_wrapper[2].removesuffix(' "$@"')
    environment = {
        "PATH": str(executable.parent) + os.pathsep + os.defpath,
        "VONK_CONTROL_HOSTNAME": "spark.acceptance.invalid",
        "CAPTURE_ARGUMENTS": str(captured),
    }
    subprocess.run(
        [*old_wrapper, *service["command"]], check=True, timeout=2, env=environment
    )
    old_selected = captured.read_text().splitlines()
    assert old_selected != service["command"][1:]
    assert (
        old_selected[old_selected.index("--config") + 1]
        == "/run/vonk-runtime-assets/caddy/Caddyfile"
    )
    # A previous signed bundle uses the same native startup boundary without
    # argv forwarding. Its fixture adaptation preserves the entire release's
    # wait script and applies only the reviewed final-argv correction.
    historical = published["services"]["caddy"]["entrypoint"][:3]
    historical[2] = historical[2].removesuffix(' "$$@"')
    adapted = lifecycle._acceptance_caddy_entrypoint(historical)
    historical_wrapper = [
        argument.replace("$$", "$").replace(
            "/run/vonk-runtime-assets/caddy/entrypoint.sh", str(native)
        )
        for argument in adapted
    ]
    subprocess.run(
        [*historical_wrapper, *service["command"]],
        check=True,
        timeout=2,
        env=environment,
    )
    assert captured.read_text().splitlines() == service["command"][1:]
    with pytest.raises(lifecycle.LifecycleError):
        lifecycle._acceptance_caddy_entrypoint(
            ["/bin/sh", "-c", "exec foreign-startup"]
        )
    subprocess.run(
        [*wrapper, *service["command"]],
        check=True,
        timeout=2,
        env={
            "PATH": str(executable.parent) + os.pathsep + os.defpath,
            "VONK_CONTROL_HOSTNAME": "spark.acceptance.invalid",
            "CAPTURE_ARGUMENTS": str(captured),
        },
    )
    assert captured.read_text().splitlines() == service["command"][1:]
    selected = captured.read_text().splitlines()
    assert selected[selected.index("--config") + 1] == "/etc/caddy/Caddyfile"
    from vonk_control.presence import ManagementAddressPolicy, PresenceError

    run = lifecycle.SparkLifecycle.__new__(lifecycle.SparkLifecycle)
    run.synthetic_fabric_octet = 42
    policy = ManagementAddressPolicy.parse(
        run._controller_site_values()["VONK_MANAGEMENT_CIDRS"]
    )
    assert policy.validate("172.31.42.1") == "172.31.42.1"
    with pytest.raises(PresenceError):
        policy.validate("172.26.0.1")
    assert policy.validate("172.31.42.1") == "172.31.42.1"


def test_synthetic_device_fixture_supports_the_arm64_spark_runner() -> None:
    lifecycle = _module()

    arm64_raw, arm64_digest = lifecycle._synthetic_device_fixture("linux-arm64")

    document = json.loads(arm64_raw)
    assert document["kind"] == "nvidia.com/gpu"
    assert document["devices"] == [
        {
            "containerEdits": {"env": ["VONK_SYNTHETIC_CDI=1"]},
            "name": "all",
        }
    ]
    assert len(arm64_digest) == 64
    for platform in ("linux-amd64", "linux-riscv64"):
        with pytest.raises(lifecycle.LifecycleError):
            lifecycle._synthetic_device_fixture(platform)


def test_synthetic_canary_lists_the_complete_recipe_catalog() -> None:
    lifecycle = _module()
    fixture = lifecycle.CanonicalCanaryFixture(
        index_path=Path("index.json"),
        index_bytes=b"{}",
        package_path=PurePosixPath("package.tar.gz"),
        package_bytes=b"package",
        source_commit="a" * 40,
        publisher="vonk-forge-test",
        slug="synthetic-canary",
        recipe_content_sha256="b" * 64,
        model_content_sha256="c" * 64,
        role="entrypoint",
        serving_check={},
        recipe={},
    )
    calls: list[tuple[str, str, dict[str, object]]] = []

    class Control:
        @staticmethod
        def request(method, path, payload=None, **kwargs):
            calls.append((method, path, kwargs))
            if method == "GET" and path == "/api/recipe/library":
                return 200, {"recipes": []}
            raise AssertionError((method, path, payload, kwargs))

    run = lifecycle.SparkLifecycle.__new__(lifecycle.SparkLifecycle)
    run.control = Control()
    run._import_canary_catalog = lambda _fixture, _request_key: {
        "state": "current",
        "commit": fixture.source_commit,
        # The canary Recipe plus its catalog model document.
        "total_count": 2,
        "processed_count": 2,
        "imported_count": 1,
        "unchanged_count": 0,
        "problems": [],
    }
    run.browser = object()
    run.synthetic_canary_fixture = fixture
    run._installation_failure = lambda _stage, error: lifecycle.LifecycleError(
        str(error)
    )

    with pytest.raises(lifecycle.LifecycleError):
        run._run_synthetic_canary("spk_" + "1" * 32)

    assert calls == [("GET", "/api/recipe/library", {})]


def test_fleet_snapshot_validates_the_decoded_response_as_json() -> None:
    pytest.importorskip(
        "sqlalchemy",
        reason="Fleet projection contract requires the control environment",
    )
    lifecycle = _module()
    expected_payload = {
        "event_cursor": 0,
        "generated_at": "2026-09-10T00:00:00Z",
        "authority_revision": "a" * 64,
        "nodes": [],
    }

    class Control:
        @staticmethod
        def request(method, path, request_payload=None, **kwargs):
            assert (method, path, request_payload, kwargs) == (
                "GET",
                "/api/fleet",
                None,
                {},
            )
            return 200, expected_payload

    run = lifecycle.SparkLifecycle.__new__(lifecycle.SparkLifecycle)
    run.control = Control()

    assert run._fleet_snapshot() == expected_payload


def test_agent_identity_wait_requires_recipe_builder_capability(monkeypatch) -> None:
    lifecycle = _module()
    node_id = "spk_" + "1" * 32
    self_test = {
        "semantic_version": "1.2.3",
        "architecture": "linux-arm64",
        "build_digest": "sha256:" + "a" * 64,
        "binary_digest": "b" * 64,
    }
    responses = iter(
        [
            {
                "id": node_id,
                "display_name": "Spark",
                "connection": {"agent_state": "active", "online_state": "online"},
                "inventory": {"freshness": "fresh", "capabilities": []},
            },
            {
                "id": node_id,
                "display_name": "Spark",
                "connection": {"agent_state": "active", "online_state": "online"},
                "inventory": {
                    "freshness": "fresh",
                    "capabilities": ["recipe.build.v1"],
                },
            },
        ]
    )

    class Control:
        def request(self, method, path):
            assert (method, path) == ("GET", "/api/fleet")
            return 200, {"nodes": [next(responses)]}

    run = lifecycle.SparkLifecycle.__new__(lifecycle.SparkLifecycle)
    run.arguments = SimpleNamespace(platform="linux-arm64")
    run.control = Control()
    run.graph = {
        "baseline_version": "0.0.0",
        "baseline_package_sha256": "c" * 64,
        "candidate_package_sha256": "d" * 64,
    }
    run._self_test = lambda: self_test
    run._installed_package_version = lambda: "1.2.3"
    run._psql = lambda _query: [
        ["linux-arm64", "1.2.3", "sha256:" + "a" * 64, "b" * 64, "1234567890"]
    ]
    monkeypatch.setattr(lifecycle.time, "sleep", lambda _seconds: None)

    identity = run._wait_for_agent_identity(package_version="1.2.3", timeout=1)

    assert identity["node_id"] == node_id
    assert identity["package_sha256"] == "d" * 64


def test_synthetic_canary_download_uses_the_current_operator_request_shape() -> None:
    from cluster_profiles.control_client import validate_control_document

    lifecycle = _module()
    run = lifecycle.SparkLifecycle.__new__(lifecycle.SparkLifecycle)
    request_key = "11111111-1111-4111-8111-111111111111"
    response = {"id": "original-download"}

    class Control:
        @staticmethod
        def request(method, path, body, *, allowed):
            assert (method, path) == (
                "POST",
                "/api/recipe/vonk-forge-test/canonical-synthetic-canary/download",
            )
            request = validate_control_document("RecipeDownloadRequest", body)
            assert request["request_key"] == request_key
            assert 202 in allowed
            return 202, response

    run.control = Control()
    assert (
        run._request_recipe_download(
            "vonk-forge-test/canonical-synthetic-canary", request_key=request_key
        )
        == response
    )


def test_canary_catalog_import_hands_the_exact_fixture_to_the_controller(
    tmp_path: Path,
) -> None:
    lifecycle = _module()
    fixture = lifecycle.CanonicalCanaryFixture(
        index_path=Path("index.json"),
        index_bytes=b'{"source_commit": "' + b"a" * 40 + b'"}',
        package_path=PurePosixPath("package.tar.gz"),
        package_bytes=bytes(range(256)),
        source_commit="a" * 40,
        publisher="vonk-forge-test",
        slug="canonical-synthetic-canary",
        recipe_content_sha256="b" * 64,
        model_content_sha256="c" * 64,
        role="entrypoint",
        serving_check={},
        recipe={},
    )
    commands: list[tuple[list[str], str | None]] = []

    def run_command(command, *, input_text=None, **_kwargs):
        commands.append((list(command), input_text))
        return subprocess.CompletedProcess(
            command, 0, stdout='warning\n{"state": "current"}\n', stderr=""
        )

    run = lifecycle.SparkLifecycle.__new__(lifecycle.SparkLifecycle)
    run.bundle = tmp_path
    run.project = "vonk-spark-1-arm64"
    run._run_command = run_command

    assert run._import_canary_catalog(fixture, "request-key") == {"state": "current"}

    [(command, input_text)] = commands
    assert command[command.index("exec") :][:7] == [
        "exec",
        "-T",
        "--user",
        "10001:10001",
        "control-api",
        "python",
        "-c",
    ]
    assert command[-1] == lifecycle.CANARY_CATALOG_IMPORT.read_text()
    assert input_text is not None
    payload = json.loads(input_text)
    assert payload["request_key"] == "request-key"
    assert payload["index"].encode() == fixture.index_bytes
    assert [base64.b64decode(value) for value in payload["packages"]] == [
        fixture.package_bytes
    ]


def test_canary_catalog_import_applies_the_producer_fixture(tmp_path: Path) -> None:
    """The in-container program imports the real canary through catalog sync."""
    pytest.importorskip("vonk_control", reason="requires the control environment")
    library = os.environ.get("VONK_RECIPE_LIBRARY_ROOT")
    if not library:
        pytest.skip("set VONK_RECIPE_LIBRARY_ROOT to the canonical recipe library")
    from datetime import UTC, datetime

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from vonk_control.auth import TokenCodec
    from vonk_control.catalog_service import CatalogService
    from vonk_control.catalog_sync import ManagedRecipeCatalogSyncService
    from vonk_control.models import Base
    from vonk_control.source_bundles import SourceBundleStore

    specification = importlib.util.spec_from_file_location(
        "spark_canary_catalog_import", _module().CANARY_CATALOG_IMPORT
    )
    assert specification is not None and specification.loader is not None
    program = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(program)
    index_path = Path(library) / "tests/fixtures/canonical-synthetic-canary/index.json"
    index = json.loads(index_path.read_text())
    package = (Path(library) / index["recipes"][0]["package"]["path"]).read_bytes()
    digest = index["recipes"][0]["package"]["sha256"]
    archive = program._cache_package(tmp_path / "packages", package, digest)
    assert archive == tmp_path / "packages" / digest[:2] / f"{digest}.tar.gz"
    engine = create_engine(f"sqlite:///{tmp_path / 'catalog.sqlite'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    clock = lambda: datetime(2026, 9, 28, tzinfo=UTC)
    reader = program.FixtureReader(index, {digest: archive})
    sync = ManagedRecipeCatalogSyncService(
        sessions,
        catalog=CatalogService(
            sessions,
            clock=clock,
            cursors=TokenCodec(b"s" * 32).cursor_codec(),
            source_bundles=SourceBundleStore(tmp_path / "bundles"),
        ),
        reader=reader,
        clock=clock,
    )

    view = program.sync_canary_catalog(
        sync,
        request_key="eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee",
        snapshot=reader.snapshot,
    )

    assert (view.state, view.commit, view.imported_count, view.problems) == (
        "current",
        index["source_commit"],
        1,
        (),
    )


@pytest.mark.parametrize("predecessor", [None, "reviewed", "unreviewed"])
def test_canary_catalog_path_calls_the_installed_service_contract(
    tmp_path, predecessor, monkeypatch
):
    """Catches keyword drift in the executable harness, without a recipe checkout.

    Predecessor facades expose their real historical signatures and forward to
    today's real service; no stub replaces its acceptance or durable sync path.
    """
    import uuid
    from datetime import UTC, datetime

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from vonk_control.auth import TokenCodec
    from vonk_control.catalog_service import CatalogService
    from vonk_control.catalog_sync import ManagedRecipeCatalogSyncService
    from vonk_control.catalog_sync_contract import (
        CatalogSyncTrigger,
        ManagedCatalogSyncRequest,
        reviewed_catalog_content,
    )
    from vonk_control.models import Base
    from vonk_control.recipe_library_types import RecipeLibrarySnapshot

    specification = importlib.util.spec_from_file_location(
        "canary_import_contract",
        ENTRY_POINT.with_name("spark_canary_catalog_import.py"),
    )
    assert specification is not None and specification.loader is not None
    program = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(program)
    engine = create_engine(f"sqlite:///{tmp_path / 'sync.sqlite'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    clock = lambda: datetime(2026, 10, 9, tzinfo=UTC)
    snapshot = RecipeLibrarySnapshot(commit="c" * 40, items=())
    reader = program.FixtureReader(
        {
            "source_commit": snapshot.commit,
            "repository": snapshot.repository,
            "recipes": [],
            "catalog_entities": [],
        },
        {},
    )
    service = ManagedRecipeCatalogSyncService(
        sessions,
        catalog=CatalogService(
            sessions, clock=clock, cursors=TokenCodec(b"s" * 32).cursor_codec()
        ),
        reader=reader,
        clock=clock,
    )

    def reviewed(*, request_key, trigger, actor, reviewed_snapshot=None):
        return service.sync(
            ManagedCatalogSyncRequest(
                request_key=request_key,
                trigger=CatalogSyncTrigger(trigger),
                actor=actor,
                reviewed_content_sha256=reviewed_catalog_content(reviewed_snapshot)
                if reviewed_snapshot is not None
                else None,
            )
        )

    def unreviewed(*, request_key, trigger, actor, expected_commit=None):
        return service.sync(
            ManagedCatalogSyncRequest(
                request_key=request_key,
                trigger=CatalogSyncTrigger(trigger),
                actor=actor,
            )
        )

    if predecessor is not None:
        from typing import Literal

        # Promoted Controllers expose a Literal rather than today's enum.
        monkeypatch.setattr(
            program,
            "SyncTrigger",
            Literal[CatalogSyncTrigger.MANUAL, CatalogSyncTrigger.AUTOMATIC],
        )

    installed = (
        service
        if predecessor is None
        else SimpleNamespace(sync=reviewed if predecessor == "reviewed" else unreviewed)
    )
    request_key = str(uuid.uuid4())
    first = program.sync_canary_catalog(
        installed, request_key=request_key, snapshot=snapshot
    )
    replay = program.sync_canary_catalog(
        installed, request_key=request_key, snapshot=snapshot
    )
    fresh = program.sync_canary_catalog(
        installed, request_key=str(uuid.uuid4()), snapshot=snapshot
    )
    assert first.completed_at is not None and not first.problems
    assert replay.id == first.id
    assert (
        fresh.id != first.id and fresh.completed_at is not None and not fresh.problems
    )


def test_synthetic_device_is_resolved_by_the_native_docker_daemon(
    tmp_path: Path,
) -> None:
    lifecycle = _module()
    run = lifecycle.SparkLifecycle.__new__(lifecycle.SparkLifecycle)
    run.bundle = tmp_path
    run.temporary_root = tmp_path
    run.project = "vonk-spark-42-arm64"
    container = "a" * 64
    image = "ghcr.io/example/caddy@sha256:" + "b" * 64
    observed: list[list[str]] = []

    def command(argv, *, cwd, timeout=300):
        assert cwd == tmp_path
        observed.append(argv)
        if argv[-3:] == ["ps", "--quiet", "caddy"]:
            stdout = container + "\n"
        elif argv[:4] == ["docker", "inspect", "--format", "{{.Config.Image}}"]:
            stdout = image + "\n"
        else:
            stdout = ""
        return subprocess.CompletedProcess(argv, 0, stdout=stdout)

    run._run_command = command

    run._verify_synthetic_docker_device()

    assert [
        "docker",
        "run",
        "--rm",
        "--name",
        "vonk-cdi-probe-vonk-spark-42-arm64",
        "--network",
        "none",
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--device",
        "nvidia.com/gpu=all",
        "--entrypoint",
        "/bin/sh",
        image,
        "-eu",
        "-c",
        'test "${VONK_SYNTHETIC_CDI:-}" = 1',
    ] in observed


def test_synthetic_controller_accepts_the_reported_fabric_subnet() -> None:
    lifecycle = _module()
    run = lifecycle.SparkLifecycle.__new__(lifecycle.SparkLifecycle)
    run.synthetic_fabric_octet = 42

    values = run._controller_site_values()

    assert values["VONK_MANAGEMENT_CIDRS"] == "172.31.42.0/30"
    assert values["VONK_DIRECT_FABRIC_CIDRS"] == "198.19.42.0/24"


def test_synthetic_firewall_preparation_only_supplies_installer_inputs(
    tmp_path: Path,
) -> None:
    lifecycle = _module()
    run = lifecycle.SparkLifecycle.__new__(lifecycle.SparkLifecycle)
    run.bundle = tmp_path
    run.temporary_root = tmp_path
    run.project = "vonk-spark-42-arm64"
    run.synthetic_interfaces = []
    run.synthetic_fabric_octet = 42
    observed: list[list[str]] = []

    def command(argv, *, cwd, timeout=300):
        assert cwd == tmp_path
        observed.append(argv)
        if argv[-3:] == ["ps", "--quiet", "litellm"]:
            stdout = "a" * 64 + "\n"
        elif argv[:2] == ["docker", "inspect"]:
            stdout = "4242\n"
        else:
            stdout = ""
        return subprocess.CompletedProcess(argv, 0, stdout=stdout)

    run._run_command = command

    run._prepare_synthetic_firewall_environment()

    assert run.firewall_environment["VONK_NAS_MANAGEMENT_IP"] == "172.31.42.2"
    assert run.firewall_environment["VONK_NODE_MANAGEMENT_IP"] == "172.31.42.1"
    assert run.firewall_environment["VONK_NODE_FABRIC_IP"] == "198.19.42.1"
    assert run.firewall_environment["VONK_PEER_FABRIC_IP"] == "198.19.42.2"
    assert len(run.synthetic_interfaces) == 2
    assert run.synthetic_interfaces[0].startswith("vmgt")
    assert run.synthetic_interfaces[1].startswith("vfab")
    assert any("172.31.42.1/30" in argv for argv in observed)
    assert any("172.31.42.2/30" in argv for argv in observed)
    assert any("/usr/bin/nsenter" in argv for argv in observed)
    assert all(
        not ("172.31.42.1/30" in argv and "198.19.42.1/24" in argv) for argv in observed
    )
    assert all("/usr/bin/install" not in argv for argv in observed)


def test_owned_management_peer_survives_gateway_namespace_replacement(
    tmp_path: Path,
) -> None:
    lifecycle = _module()
    run = lifecycle.SparkLifecycle.__new__(lifecycle.SparkLifecycle)
    run.bundle = tmp_path
    run.temporary_root = tmp_path
    run.project = "vonk-spark-42-arm64"
    run.synthetic_interfaces = []
    run.synthetic_fabric_octet = 42
    observed: list[list[str]] = []
    generation = 0
    foreign_host = False

    def command(argv, *, cwd, timeout=300):
        assert cwd == tmp_path
        observed.append(argv)
        if argv[-3:] == ["ps", "--quiet", "litellm"]:
            stdout = ("a" if generation == 0 else "b") * 64 + "\n"
        elif argv[:2] == ["docker", "inspect"]:
            stdout = "4242\n" if generation == 0 else "5151\n"
        elif "address" in argv and "show" in argv:
            interface = argv[-1]
            peer = interface.startswith("vnas")
            address = "172.31.42.2" if peer else "172.31.42.1"
            if foreign_host and not peer:
                address = "172.26.0.1"
            stdout = json.dumps(
                [
                    {
                        "ifname": interface,
                        "addr_info": [
                            {"family": "inet", "local": address, "prefixlen": 30}
                        ],
                    }
                ]
            )
        else:
            stdout = ""
        return subprocess.CompletedProcess(argv, 0, stdout=stdout)

    run._run_command = command
    run._prepare_synthetic_firewall_environment()
    original_environment = dict(run.firewall_environment)
    original_interfaces = list(run.synthetic_interfaces)
    observed.clear()
    run._detach_synthetic_management_peer()
    generation = 1  # Compose replaces the gateway; accepted Start is unchanged.
    run._attach_synthetic_management_peer()
    moves = [argv for argv in observed if "link" in argv and "netns" in argv]
    assert len(moves) == 2
    assert moves[0][2:6] == ["--target", "4242", "--net", "/usr/sbin/ip"]
    assert moves[0][-1] == str(lifecycle.os.getpid())
    assert moves[1][-1] == "5151"
    assert all(argv[-3].startswith("vnas") for argv in moves)
    assert run.synthetic_management_owner == ("b" * 64, "5151")
    assert run.synthetic_interfaces == original_interfaces
    assert run.firewall_environment == original_environment
    # Replacing the gateway never deletes/recreates the accepted host device,
    # changes its .1 address, or touches the independent fabric interface.
    mutations = [argv for argv in observed if "set" in argv or "replace" in argv]
    assert all(original_interfaces[0] not in argv for argv in mutations)
    assert all(original_interfaces[1] not in argv for argv in mutations)
    assert not any("delete" in argv or "add" in argv for argv in observed)
    observed.clear()
    foreign_host = True
    with pytest.raises(lifecycle.LifecycleError):
        run._detach_synthetic_management_peer()
    assert not any("netns" in argv or "set" in argv for argv in observed)


def test_cleanup_targets_only_the_exact_compose_project_and_its_volumes(
    tmp_path: Path,
) -> None:
    lifecycle = _module()
    root = tmp_path / "run"
    bundle = root / "bundle"
    bundle.mkdir(parents=True)
    observed: list[tuple[list[str], Path, int]] = []
    run = lifecycle.SparkLifecycle.__new__(lifecycle.SparkLifecycle)
    run.bundle = bundle
    run.project = "vonk-spark-42-arm64"
    run.temporary_root = root
    run.synthetic_paths = [Path("/etc/cdi/vonk-spark-acceptance.json")]
    run.synthetic_interfaces = ["vmgt99999"]
    run._run_command = lambda command, *, cwd, timeout=300: observed.append(
        (command, cwd, timeout)
    )

    lifecycle.__dict__["_agent_package_installed"] = lambda: False
    reclaimed = []

    def reclaim(target):
        assert target == bundle
        assert "down" in observed[-1][0]
        assert root.exists()
        reclaimed.append(target)

    lifecycle.__dict__["reclaim_gateway_journal"] = reclaim
    run._cleanup()

    assert reclaimed == [bundle]
    assert not root.exists()
    assert run.temporary_root is None
    root.mkdir()  # The next acceptance run can use its own fresh directory.

    assert observed == [
        (
            [
                "sudo",
                "/usr/bin/rm",
                "-f",
                "--",
                "/etc/cdi/vonk-spark-acceptance.json",
            ],
            Path("/"),
            30,
        ),
        (
            [
                "sudo",
                "/usr/bin/rm",
                "-rf",
                "--",
                "/etc/vonk-forge-agent",
                "/var/lib/vonk-forge-agent",
            ],
            Path("/"),
            60,
        ),
        (
            [
                "docker",
                "compose",
                "--project-name",
                "vonk-spark-42-arm64",
                "down",
                "--volumes",
                "--remove-orphans",
                "--timeout",
                "30",
            ],
            bundle,
            120,
        ),
    ]


def test_local_browser_controller_uses_only_the_loopback_publication(
    monkeypatch,
) -> None:
    lifecycle = _module()
    observed: dict[str, object] = {}

    class Response:
        status = 200

        @staticmethod
        def getheaders():
            return [("Content-Type", "application/json")]

        @staticmethod
        def read(limit):
            observed["limit"] = limit
            return b"{}"

    class Connection:
        def __init__(self, host, port, *, timeout):
            observed.update(host=host, port=port, timeout=timeout)

        def request(self, method, path, *, body, headers):
            observed.update(method=method, path=path, body=body, headers=headers)

        @staticmethod
        def getresponse():
            return Response()

        @staticmethod
        def close():
            observed["closed"] = True

    monkeypatch.setattr(lifecycle.http.client, "HTTPConnection", Connection)
    boundary = lifecycle.LocalBrowserController(
        hostname="vonk-forge.acceptance.example.test",
        port=49152,
    )

    assert boundary.raw_request("GET", "/healthz", None, {}, 5) == (
        200,
        {"content-type": ["application/json"]},
        b"{}",
    )
    assert observed == {
        "host": "127.0.0.1",
        "port": 49152,
        "timeout": 5,
        "method": "GET",
        "path": "/healthz",
        "body": None,
        "headers": {"Host": "vonk-forge.acceptance.example.test"},
        "limit": lifecycle.MAXIMUM_RESPONSE_BYTES + 1,
        "closed": True,
    }

    observed.clear()
    assert boundary.bearer("opaque-inference-key", timeout=5).request(
        "GET", "/healthz"
    ) == (200, {})
    assert observed["headers"] == {
        "Accept": "application/json",
        "Authorization": "Bearer opaque-inference-key",
        "Content-Type": "application/json",
        "Host": "vonk-forge.acceptance.example.test",
    }


def test_local_browser_port_is_discovered_from_the_isolated_project(
    tmp_path: Path,
) -> None:
    lifecycle = _module()
    run = lifecycle.SparkLifecycle.__new__(lifecycle.SparkLifecycle)
    run.bundle = tmp_path
    run.project = "vonk-spark-42-arm64"
    observed: list[list[str]] = []

    def command(argv, *, cwd, timeout=300):
        observed.append(argv)
        assert cwd == tmp_path
        return subprocess.CompletedProcess(argv, 0, stdout="127.0.0.1:49152\n")

    run._run_command = command

    assert run._local_browser_port() == 49152
    assert observed == [
        [
            "docker",
            "compose",
            "--project-name",
            "vonk-spark-42-arm64",
            "port",
            "caddy",
            "8080",
        ]
    ]


def test_parallel_spark_lanes_reject_every_tailnet_input(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lifecycle = _module()
    for name in lifecycle.FORBIDDEN_SPARK_TAILNET_INPUTS:
        monkeypatch.delenv(name, raising=False)

    monkeypatch.delenv("VONK_ACCEPTANCE_SPARK_CONTROLLER_BOUNDARY", raising=False)
    with pytest.raises(lifecycle.LifecycleError):
        lifecycle._require_loopback_controller_boundary()

    monkeypatch.setenv("VONK_ACCEPTANCE_SPARK_CONTROLLER_BOUNDARY", "tailnet")
    with pytest.raises(lifecycle.LifecycleError):
        lifecycle._require_loopback_controller_boundary()

    monkeypatch.setenv("VONK_ACCEPTANCE_SPARK_CONTROLLER_BOUNDARY", "loopback")
    monkeypatch.setenv(
        "VONK_ACCEPTANCE_TAILSCALE_OAUTH_CLIENT_SECRET", "must-not-be-visible"
    )
    with pytest.raises(lifecycle.LifecycleError):
        lifecycle._require_loopback_controller_boundary()

    monkeypatch.delenv("VONK_ACCEPTANCE_TAILSCALE_OAUTH_CLIENT_SECRET")
    lifecycle._require_loopback_controller_boundary()


def test_parallel_spark_controller_start_cannot_create_tailscale_services() -> None:
    lifecycle = _module()
    run = lifecycle.SparkLifecycle.__new__(lifecycle.SparkLifecycle)
    run.project = "vonk-spark-42-arm64"

    assert lifecycle.LOCAL_CONTROLLER_SERVICES == lifecycle.DEFAULT_SERVICES - {
        "tailscale-configurator",
        "tailscale-gateway",
    }
    command = run._local_controller_up_command()
    assert command == [
        "docker",
        "compose",
        "--project-name",
        "vonk-spark-42-arm64",
        "up",
        "-d",
        "--wait",
        "--wait-timeout",
        "360",
        "--remove-orphans",
        *sorted(lifecycle.LOCAL_CONTROLLER_SERVICES),
    ]
    assert not lifecycle.TAILSCALE_CONTROLLER_SERVICES & set(command)


def test_spark_project_identity_is_arm64_only() -> None:
    lifecycle = _module()

    arm64 = lifecycle._spark_project_identity(42, "linux-arm64")

    assert arm64 == "vonk-spark-42-arm64"
    for platform in ("linux-amd64", "linux-unknown"):
        with pytest.raises(lifecycle.LifecycleError):
            lifecycle._spark_project_identity(42, platform)


def test_enrollment_grant_requires_the_installer_route_metadata() -> None:
    from uuid import UUID

    from cluster_profiles.generated_control.models.fleet_enroll_request import (
        FleetEnrollRequest,
    )

    lifecycle = _module()
    run = lifecycle.SparkLifecycle.__new__(lifecycle.SparkLifecycle)
    run.control_hostname = "vonk-forge-acceptance.tailnet.example"
    run.arguments = SimpleNamespace(channel="dev")
    grant = {
        "ca_fingerprint": "a" * 64,
        "controller_address": "127.0.0.1",
        "controller_endpoint": "https://agents.vonk-forge-spark-local.spark.acceptance.invalid:8443",
        "enrollment_endpoint": "https://enroll.vonk-forge-spark-local.spark.acceptance.invalid:8443",
        "expires_at": "2026-08-22T20:00:00Z",
        "id": "11111111-1111-1111-1111-111111111111",
        "installer_url": "https://install.vonkforge.ai/dev/spark",
        "purpose": "new-node",
        "service_hostnames": [
            "vonk-forge-acceptance.tailnet.example",
            "enroll.vonk-forge-spark-local.spark.acceptance.invalid",
            "agents.vonk-forge-spark-local.spark.acceptance.invalid",
            "registry.vonk-forge-spark-local.spark.acceptance.invalid",
        ],
        "token": "t" * 43,
    }

    class Control:
        @staticmethod
        def request(method, path, body):
            assert (method, path) == ("POST", "/api/fleet/enroll")
            request = FleetEnrollRequest.from_dict(body)
            assert UUID(request.request_key).version == 4
            assert request.name == "Acceptance Spark"
            return 201, {"grant": dict(grant)}

    run.control = Control()

    assert run._create_grant() == (
        grant["id"],
        grant["enrollment_endpoint"],
        grant["ca_fingerprint"],
        grant["token"],
    )

    invalid = dict(grant, installer_url="https://install.vonkforge.ai/spark")
    Control.request = staticmethod(lambda method, path, body: (201, {"grant": invalid}))
    with pytest.raises(lifecycle.LifecycleError):
        run._create_grant()


def test_installer_environment_routes_spark_bootstrap_to_acceptance_controller(
    tmp_path: Path,
) -> None:
    lifecycle = _module()
    candidate = tmp_path / "candidate/release.json"
    baseline = tmp_path / "baseline/release.json"
    for release in (candidate, baseline):
        release.parent.mkdir(parents=True)
        release.write_text("{}")
        (release.parent / "release.sig").write_text("signature")
    run = lifecycle.SparkLifecycle.__new__(lifecycle.SparkLifecycle)
    run.temporary_root = tmp_path
    run.origin = "https://install.example"
    run.arguments = SimpleNamespace(
        baseline_release=baseline,
        candidate_release=candidate,
        channel="dev",
        generation="a" * 64,
    )

    candidate_environment = run._installer_environment(baseline=False)
    baseline_environment = run._installer_environment(baseline=True)

    assert candidate_environment["VONK_CONTROLLER_ADDRESS"] == "127.0.0.1"
    assert baseline_environment["VONK_CONTROLLER_ADDRESS"] == "127.0.0.1"
    assert candidate_environment["VONK_INSTALL_BASE_URL"].endswith("/" + "a" * 64)
    assert baseline_environment["VONK_INSTALL_BASE_URL"].endswith(
        "/" + "a" * 64 + "/acceptance-baseline"
    )


def test_controller_startup_diagnostics_are_bounded_and_redact_secrets(
    tmp_path: Path, monkeypatch
) -> None:
    lifecycle = _module()
    run = lifecycle.SparkLifecycle.__new__(lifecycle.SparkLifecycle)
    run.bundle = tmp_path
    run.project = "vonk-spark-42-arm64"
    secret = "tskey-client-sensitive-value"
    monkeypatch.setenv("VONK_ACCEPTANCE_TAILSCALE_OAUTH_CLIENT_SECRET", secret)
    status = subprocess.CompletedProcess(
        [],
        0,
        stdout=json.dumps(
            [
                {
                    "ExitCode": 0,
                    "Health": "healthy",
                    "Service": "postgres",
                    "State": "running",
                },
                {
                    "ExitCode": 1,
                    "Health": "",
                    "Service": "tailscale-gateway",
                    "State": "restarting",
                },
            ]
        ),
        stderr="",
    )
    logs = subprocess.CompletedProcess(
        [],
        0,
        stdout=f"discarded diagnostic beginning{'x' * 9_000}\nauthentication failed for {secret}\n",
        stderr="",
    )
    outputs = iter((status, logs))
    run._diagnostic_command = lambda _command: next(outputs)

    diagnostics = run._controller_startup_diagnostics()

    assert secret not in diagnostics
    assert "discarded diagnostic beginning" not in diagnostics
    assert len(diagnostics) < 8_500
    assert "authentication failed for <redacted>" in diagnostics
    assert "postgres=running/healthy/exit-0" in diagnostics
    assert "tailscale-gateway=restarting/none/exit-1" in diagnostics


def test_diagnostics_redact_enrollment_jwt_from_ca_logs() -> None:
    lifecycle = _module()
    token = "eyJhbGciOiJFUzI1NiJ9.eyJzdWIiOiJmaXh0dXJlIn0.fixtureSignature"
    output = lifecycle.SparkLifecycle._redact_diagnostics(
        json.dumps({"ott": token, "status": 201})
    )
    assert token not in output
    assert json.loads(output) == {"ott": "<redacted-jwt>", "status": 201}


def test_installer_failure_diagnostics_are_bounded_and_redact_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lifecycle = _module()
    run = lifecycle.SparkLifecycle.__new__(lifecycle.SparkLifecycle)
    secret = "tskey-client-sensitive-value"
    monkeypatch.setenv("VONK_ACCEPTANCE_TAILSCALE_OAUTH_CLIENT_SECRET", secret)
    error = lifecycle.AcceptanceError(
        f"discarded diagnostic beginning{'x' * 9_000}\n"
        f"setup command failed for {secret}\n"
    )

    failure = run._installation_failure("baseline Spark installation", error)
    rendered = str(failure)

    assert secret not in rendered
    assert "discarded diagnostic beginning" not in rendered
    assert len(rendered) <= run._DIAGNOSTIC_BUDGET + len(
        "baseline Spark installation failed; "
    )
    assert "baseline Spark installation failed" in rendered
    assert "setup command failed for <redacted>" in rendered


def test_installer_failure_includes_redacted_controller_diagnostics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lifecycle = _module()
    run = lifecycle.SparkLifecycle.__new__(lifecycle.SparkLifecycle)
    run.bundle = tmp_path
    run.project = "vonk-spark-42-arm64"
    secret = "tskey-client-sensitive-value"
    monkeypatch.setenv("VONK_ACCEPTANCE_TAILSCALE_OAUTH_CLIENT_SECRET", secret)
    observed: list[list[str]] = []

    def diagnostics(command):
        observed.append(command)
        message = (
            "helper request rejected"
            if "journalctl" in command
            else "control enrollment failed"
        )
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=f"{message} for {secret}\n",
            stderr="",
        )

    run._diagnostic_command = diagnostics

    failure = run._installation_failure(
        "baseline Spark installation", lifecycle.AcceptanceError("Error: Status(500)")
    )

    assert secret not in str(failure)
    assert "Error: Status(500)" in str(failure)
    assert "control enrollment failed for <redacted>" in str(failure)
    assert "helper request rejected for <redacted>" in str(failure)
    assert observed == [
        # Service states first: they are what tells a stuck queue apart from a
        # worker that is not draining it.
        [
            "docker",
            "compose",
            "--project-name",
            "vonk-spark-42-arm64",
            "ps",
            "--all",
            "--format",
            "json",
        ],
        # Then each service's own log tail, worker first, rather than one
        # interleaved tail whose last lines are whatever is noisiest.
        [
            "docker",
            "compose",
            "--project-name",
            "vonk-spark-42-arm64",
            "logs",
            "--no-color",
            "--tail",
            "80",
            "control-worker",
        ],
        [
            "docker",
            "compose",
            "--project-name",
            "vonk-spark-42-arm64",
            "logs",
            "--no-color",
            "--tail",
            "80",
            "control-api",
        ],
        [
            "sudo",
            "journalctl",
            "--no-pager",
            "--lines=40",
            "--unit=vonk-forge-agent.service",
        ],
        [
            "sudo",
            "journalctl",
            "--no-pager",
            "--lines=40",
            "--unit=vonk-forge-package-helper.service",
        ],
    ]


def test_installer_error_survives_bounded_controller_diagnostics(
    tmp_path: Path,
) -> None:
    lifecycle = _module()
    run = lifecycle.SparkLifecycle.__new__(lifecycle.SparkLifecycle)
    run.bundle = tmp_path
    run.project = "vonk-spark-42-arm64"
    run._diagnostic_command = lambda command: subprocess.CompletedProcess(
        command,
        0,
        stdout="controller-log\n" * 1_000,
        stderr="",
    )

    failure = run._installation_failure(
        "baseline Spark installation", lifecycle.AcceptanceError("Error: Certificate")
    )

    rendered = str(failure)
    assert len(rendered) <= run._DIAGNOSTIC_BUDGET + len(
        "baseline Spark installation failed; "
    )
    assert "Error: Certificate" in rendered


def test_profile_application_failure_is_typed_and_redacts_provider_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pytest.importorskip(
        "fastapi", reason="Controller contract tests run in the control suite"
    )
    lifecycle = _module()
    run = lifecycle.SparkLifecycle.__new__(lifecycle.SparkLifecycle)
    run.control = object()
    secret = "acceptance-provider-secret"
    monkeypatch.setenv("VONK_ACCEPTANCE_LITELLM_UPSTREAM_KEY", secret)
    operation = _failed_profile_application(f"container-build rejected {secret}")

    with pytest.raises(lifecycle.LifecycleError) as failure:
        run._await_profile_application(
            operation,
            label="synthetic canary profile load",
            node_id="spk_" + "a" * 32,
        )

    message = str(failure.value)
    assert "container-build" in message
    assert "<redacted>" in message
    assert secret not in message


def test_profile_failure_cannot_erase_failure_or_service_journals(
    tmp_path: Path,
) -> None:
    pytest.importorskip(
        "fastapi", reason="Controller contract tests run in the control suite"
    )
    lifecycle = _module()
    run = lifecycle.SparkLifecycle.__new__(lifecycle.SparkLifecycle)
    run.control = object()
    run.bundle = tmp_path
    run.project = "vonk-spark-42-arm64"

    def diagnostics(command):
        if "--unit=vonk-forge-package-helper.service" in command:
            message = "model ACL: Read-only file system"
        elif "--unit=vonk-forge-agent.service" in command:
            message = "start job rejected"
        else:
            message = "worker start operation failed"
        return subprocess.CompletedProcess(
            command, 0, stdout="x" * 20_000 + message, stderr=""
        )

    run._diagnostic_command = diagnostics
    with pytest.raises(lifecycle.LifecycleError) as failure:
        run._await_profile_application(
            _failed_profile_application(
                "container runtime could not start the workload"
            ),
            label="synthetic canary profile load",
            node_id="spk_" + "a" * 32,
        )
    rendered = str(run._installation_failure("synthetic canary", failure.value))
    assert len(rendered) <= run._DIAGNOSTIC_BUDGET + len("synthetic canary failed; ")
    assert "container runtime could not start the workload" in rendered
    assert "model ACL: Read-only file system" in rendered
    assert "start job rejected" in rendered
    assert "worker start operation failed" in rendered


def test_direct_health_and_protected_identity_hash_are_observed_from_native_binary() -> (
    None
):
    lifecycle = _module()
    run = lifecycle.SparkLifecycle.__new__(lifecycle.SparkLifecycle)
    run._self_test = dict
    observed: list[list[str]] = []

    def command(argv, *, cwd, timeout):
        observed.append(argv)
        return subprocess.CompletedProcess(
            argv,
            0,
            stdout=f"{'a' * 64}  {lifecycle.SPARK_CONFIG}\n",
            stderr="",
        )

    run._run_command = command

    assert run._direct_agent_health() == {
        "healthy": True,
        "implementation": "rust",
        "transport": "direct",
    }
    assert run._hash_path(lifecycle.SPARK_CONFIG) == "a" * 64
    assert observed == [
        [
            "sudo",
            "/usr/bin/sha256sum",
            "--",
            "/etc/vonk-forge-agent/agent.toml",
        ]
    ]
    with pytest.raises(lifecycle.LifecycleError):
        run._hash_path(Path("/tmp/not-installation-identity"))


def test_renewal_requires_new_active_serial_and_real_old_identity_rejection() -> None:
    from vonk_agent_protocol.agent_state import (
        NativeRenewalClock,
        NativeRenewalEvidence,
    )

    lifecycle = _module()
    run = lifecycle.SparkLifecycle.__new__(lifecycle.SparkLifecycle)
    node_id = "spk_" + "1" * 32
    serial_before = str(int("1234567890abcdef", 16))
    serial_after = str(int("abcdef1234567890", 16))
    run.graph = {"candidate_version": "1.2.3"}
    triggers: list[str] = []

    def native_renewal(_deadline):
        triggers.append("native")
        return NativeRenewalEvidence(
            scheduling_clock=NativeRenewalClock.CERTIFICATE_DERIVED,
            wall_clock_utc="2026-09-28T00:00:00Z",
            scheduling_clock_utc="2026-09-28T00:00:00Z",
            source_agent_binary_sha256="a" * 64,
            source_agent_build_digest="sha256:" + "b" * 64,
            source_certificate_sha256="c" * 64,
            source_public_key_sha256="d" * 64,
            source_lifetime_seconds=3600,
            replacement_certificate_sha256="e" * 64,
            replacement_public_key_sha256="f" * 64,
            replacement_lifetime_seconds=7200,
        )

    run._exercise_native_renewal = native_renewal
    run._psql = lambda _query: [[serial_after, "revoked", "1"]]
    run._wait_for_agent_identity = lambda **_kwargs: {
        "node_id": node_id,
        "serial": serial_after,
    }
    rejected_serials: list[str] = []
    run._old_certificate_rejected = lambda serial, _serial_after: (
        rejected_serials.append(serial) is None
    )

    observed = run._observe_renewal(node_id, serial_before)
    assert triggers == ["native"]

    assert observed == {
        "node_id": node_id,
        "proof": {
            "certificate_serial_after": "abcdef1234567890",
            "certificate_serial_before": "1234567890abcdef",
            "old_certificate_rejection": {
                "durably_recorded": True,
                "rejected": True,
                "serial": "1234567890abcdef",
            },
        },
    }
    assert rejected_serials == [serial_before]


def test_openssl_ed25519_probe_key_conversion_is_strict() -> None:
    lifecycle = _module()
    seed = bytes(range(32))
    public = bytes(range(32, 64))
    source_der = (
        lifecycle.ED25519_PKCS8_V2_PREFIX
        + seed
        + lifecycle.ED25519_PKCS8_V2_PUBLIC_PREFIX
        + public
    )
    source = (
        b"-----BEGIN PRIVATE KEY-----\n"
        + base64.b64encode(source_der)
        + b"\n-----END PRIVATE KEY-----\n"
    )

    converted = lifecycle._openssl_compatible_ed25519_private_key(source)
    converted_der = base64.b64decode(b"".join(converted.splitlines()[1:-1]))

    assert converted_der == lifecycle.ED25519_PKCS8_V1_PREFIX + source_der[5:48]
    with pytest.raises(lifecycle.LifecycleError):
        lifecycle._openssl_compatible_ed25519_private_key(
            source.replace(b"PRIVATE KEY", b"RSA PRIVATE KEY")
        )


@pytest.mark.parametrize("observed", ("sha256:expected", "sha256:different"))
def test_running_channel_alias_must_match_the_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, observed: str
) -> None:
    lifecycle = _module()
    run = lifecycle.SparkLifecycle.__new__(lifecycle.SparkLifecycle)
    run.bundle = tmp_path
    run.project = "vonk-channel-test"
    run.arguments = SimpleNamespace(candidate_release=tmp_path / "release.json")
    monkeypatch.setattr(lifecycle, "COMPOSE_IMAGE_ROLES", {"api": "control-api"})
    monkeypatch.setattr(
        lifecycle,
        "_read_canonical_document",
        lambda *args: {
            "images": {"api": "ghcr.io/carstvaartjes/vonk-forge-api@sha256:candidate"}
        },
    )

    def command(argv, **kwargs):
        if argv[:2] == ["docker", "inspect"]:
            output = observed
        elif argv[:3] == ["docker", "image", "inspect"]:
            output = "sha256:expected"
        else:
            output = "container-id"
        return subprocess.CompletedProcess(argv, 0, output + "\n", "")

    run._run_command = command
    if observed == "sha256:expected":
        run._assert_running_publication_images()
    else:
        with pytest.raises(lifecycle.LifecycleError):
            run._assert_running_publication_images()


def test_preflight_failure_reports_only_projected_receipt_comparison_fields() -> None:
    pytest.importorskip(
        "fastapi", reason="Controller contract tests run in the control suite"
    )
    lifecycle = _module()
    run = lifecycle.SparkLifecycle.__new__(lifecycle.SparkLifecycle)
    run.control = object()
    evidence = {
        "node_id": "spk_" + "a" * 32,
        "current_fingerprint": "b" * 64,
        "receipt_fingerprint": "c" * 64,
        "request_sha256": "d" * 64,
        "payload_sha256": "d" * 64,
        "observed_at": 100,
        "controller_now": 102,
        "failed_findings": None,
    }
    queries = []

    def psql(query):
        queries.append(query)
        return [[json.dumps(evidence)]] if "receipt_fingerprint" in query else []

    run._psql = psql
    operation = _failed_profile_application("runtime_preflight.retry_exhausted")
    with pytest.raises(lifecycle.LifecycleError) as failure:
        run._await_profile_application(
            operation, label="canary", node_id=evidence["node_id"]
        )
    message = str(failure.value)
    assert evidence["current_fingerprint"] in message
    assert evidence["receipt_fingerprint"] in message
    assert '"observed_at": 100' in message
    assert '"controller_now": 102' in message
    assert len(message) < 2000
    assert len(queries) == 2
    assert "LIMIT 2" in queries[0]
    assert "attempt_state" in queries[1]
    assert "signed_grant" not in queries[0]


def test_profile_run_switch_receipt_is_required_for_successful_execution() -> None:
    lifecycle = _module()
    with pytest.raises(lifecycle.LifecycleError):
        lifecycle.SparkLifecycle._profile_run_switch_result(
            {"0": {"operation_id": "11111111-1111-4111-8111-111111111111"}}
        )
    receipt = {"phase_results": [], "completed_phases": ["final_verify"]}
    assert (
        lifecycle.SparkLifecycle._profile_run_switch_result(
            {"4": {"result": {"run_switch": receipt}}}
        )
        == receipt
    )


def test_recipe_download_consumes_typed_terminal_receipt() -> None:
    pytest.importorskip(
        "fastapi", reason="Controller contract tests run in the control suite"
    )
    lifecycle = _module()
    from vonk_agent_protocol import OperationProgress
    from vonk_control.recipe_availability_intent import RecipeSelectorIntent
    from vonk_control.recipe_image_availability_api import (
        RecipeImageAvailabilityResponse,
        RecipeImageAvailabilityResult,
    )

    recipe_digest = "a" * 64
    model_digest = "b" * 64
    archive_digest = "c" * 64
    image_digest = "sha256:" + "d" * 64
    terminal = RecipeImageAvailabilityResponse(
        id="11111111-1111-4111-8111-111111111111",
        request_id="22222222-2222-4222-8222-222222222222",
        request=RecipeSelectorIntent(
            selector="acceptance/synthetic-canary", force=True
        ),
        kind="recipe.image.availability.v2",
        state=LifecycleState.SUCCEEDED,
        attempt=1,
        recipe_revision_id="33333333-3333-4333-8333-333333333333",
        recipe_content_sha256=recipe_digest,
        progress=OperationProgress(phase="available"),
        result=RecipeImageAvailabilityResult(
            recipe_content_sha256=recipe_digest,
            model_content_digests=[model_digest],
            image_digest=image_digest,
            oci_archive_sha256=archive_digest,
            image_bytes=3,
        ),
        created_at="2026-09-10T10:00:00Z",
        updated_at="2026-09-10T10:00:01Z",
    )
    fixture = lifecycle.CanonicalCanaryFixture(
        index_path=Path("index.json"),
        index_bytes=b"{}",
        package_path=PurePosixPath("package.tar.gz"),
        package_bytes=b"package",
        source_commit="e" * 40,
        publisher="acceptance",
        slug="synthetic-canary",
        recipe_content_sha256=recipe_digest,
        model_content_sha256=model_digest,
        role="entrypoint",
        serving_check={},
        recipe={},
    )
    run = lifecycle.SparkLifecycle.__new__(lifecycle.SparkLifecycle)
    run.control = object()

    observed = run._await_recipe_download(
        terminal.model_dump(mode="json"),
        fixture=fixture,
        recipe_revision_id=terminal.recipe_revision_id,
    )

    assert observed["state"] == "succeeded"
    assert observed["result"]["image_digest"] == image_digest
    assert observed["result"]["model_content_digests"] == [model_digest]


def test_recipe_download_timeout_preserves_durable_progress(monkeypatch) -> None:
    pytest.importorskip(
        "fastapi", reason="Controller contract tests run in the control suite"
    )
    lifecycle = _module()
    from vonk_agent_protocol import OperationProgress
    from vonk_control.recipe_availability_intent import RecipeSelectorIntent
    from vonk_control.recipe_image_availability_api import (
        RecipeImageAvailabilityResponse,
    )

    recipe_digest = "a" * 64
    pending = RecipeImageAvailabilityResponse(
        id="11111111-1111-4111-8111-111111111111",
        request_id="22222222-2222-4222-8222-222222222222",
        request=RecipeSelectorIntent(
            selector="acceptance/synthetic-canary", force=False
        ),
        kind="recipe.image.availability.v2",
        state=LifecycleState.RUNNING,
        attempt=2,
        recipe_revision_id="33333333-3333-4333-8333-333333333333",
        recipe_content_sha256=recipe_digest,
        progress=OperationProgress(phase="build", completed_bytes=4),
        updated_at="2026-09-10T10:00:04Z",
        created_at="2026-09-10T10:00:00Z",
    )
    fixture = lifecycle.CanonicalCanaryFixture(
        index_path=Path("index.json"),
        index_bytes=b"{}",
        package_path=PurePosixPath("package.tar.gz"),
        package_bytes=b"package",
        source_commit="e" * 40,
        publisher="acceptance",
        slug="synthetic-canary",
        recipe_content_sha256=recipe_digest,
        model_content_sha256="b" * 64,
        role="entrypoint",
        serving_check={},
        recipe={},
    )
    run = lifecycle.SparkLifecycle.__new__(lifecycle.SparkLifecycle)
    run.control = object()
    monkeypatch.setattr(lifecycle, "_CANARY_CONVERGENCE_SECONDS", 0)

    with pytest.raises(
        lifecycle.LifecycleError,
    ):
        run._await_recipe_download(
            pending.model_dump(mode="json"),
            fixture=fixture,
            recipe_revision_id=pending.recipe_revision_id,
        )


def test_profile_application_poll_pins_exact_identity(monkeypatch) -> None:
    pytest.importorskip(
        "fastapi", reason="Controller contract tests run in the control suite"
    )
    lifecycle = _module()
    pending = _failed_profile_application("pending")
    pending["state"] = "queued"
    pending["status_reason"] = None
    terminal = _failed_profile_application("")
    terminal.update(
        state="succeeded",
        status_reason=None,
        total_steps=0,
        progress={"attempt": 1, "completed_steps": 0, "total_steps": 0},
        result={"changed": False, "completed_steps": 0},
    )
    calls: list[tuple[str, str]] = []

    class Control:
        @staticmethod
        def request(method, path, payload=None, **kwargs):
            calls.append((method, path))
            assert payload is None
            return 200, terminal

    run = lifecycle.SparkLifecycle.__new__(lifecycle.SparkLifecycle)
    run.control = Control()
    monkeypatch.setattr(lifecycle.time, "sleep", lambda _seconds: None)

    assert (
        run._await_profile_application(
            pending,
            label="profile load",
            node_id="spk_" + "a" * 32,
        )["state"]
        == "succeeded"
    )
    assert calls == [("GET", f"/api/profile/applications/{pending['id']}")]


def test_profile_application_poll_follows_durable_admission_wait(monkeypatch) -> None:
    pytest.importorskip(
        "fastapi", reason="Controller contract tests run in the control suite"
    )
    lifecycle = _module()
    pending = _failed_profile_application(
        "Profile admission is waiting for the active workload owner to finish."
    )
    pending["state"] = "waiting-for-operator"
    terminal = _failed_profile_application("")
    terminal.update(
        attempt=2,
        state="succeeded",
        status_reason=None,
        total_steps=0,
        progress={"attempt": 2, "completed_steps": 0, "total_steps": 0},
        result={"changed": False, "completed_steps": 0},
    )
    calls: list[tuple[str, str]] = []

    class Control:
        @staticmethod
        def request(method, path, payload=None, **kwargs):
            calls.append((method, path))
            assert payload is None
            return 200, terminal

    run = lifecycle.SparkLifecycle.__new__(lifecycle.SparkLifecycle)
    run.control = Control()
    monkeypatch.setattr(lifecycle.time, "sleep", lambda _seconds: None)

    assert (
        run._await_profile_application(
            pending,
            label="profile load",
            node_id="spk_" + "a" * 32,
        )["state"]
        == "succeeded"
    )
    assert calls == [("GET", f"/api/profile/applications/{pending['id']}")]


def test_profile_application_poll_rejects_a_different_returned_identity(
    monkeypatch,
) -> None:
    pytest.importorskip(
        "fastapi", reason="Controller contract tests run in the control suite"
    )
    lifecycle = _module()
    pending = _failed_profile_application("pending")
    pending["state"] = "queued"
    pending["status_reason"] = None
    redirected = _failed_profile_application("")
    redirected.update(
        id="cccccccc-cccc-4ccc-8ccc-cccccccccccc",
        state="succeeded",
        status_reason=None,
        total_steps=0,
        progress={"attempt": 1, "completed_steps": 0, "total_steps": 0},
        result={"changed": False, "completed_steps": 0},
    )
    calls: list[tuple[str, str]] = []

    class Control:
        @staticmethod
        def request(method, path, payload=None, **kwargs):
            calls.append((method, path))
            assert payload is None
            return 200, redirected

    run = lifecycle.SparkLifecycle.__new__(lifecycle.SparkLifecycle)
    run.control = Control()
    monkeypatch.setattr(lifecycle.time, "sleep", lambda _seconds: None)

    with pytest.raises(lifecycle.LifecycleError):
        run._await_profile_application(
            pending,
            label="profile load",
            node_id="spk_" + "a" * 32,
        )
    assert calls == [("GET", f"/api/profile/applications/{pending['id']}")]


@pytest.mark.parametrize(
    ("code", "status", "detail", "accepted"),
    [
        (0, "401", "", True),
        (
            56,
            "000",
            "curl: (56) OpenSSL SSL_read: OpenSSL/3.0.13: error:0A000415:SSL routines::sslv3 alert certificate expired, errno 0",
            True,
        ),
        (0, "404", "", False),
        (0, "500", "", False),
        (56, "000", "curl: (56) Recv failure: Connection reset by peer", False),
        (56, "000", "curl: (56) SSL_read: SSL routines::tlsv1 alert unknown ca", False),
        (
            60,
            "000",
            "curl: (60) SSL certificate problem: certificate has expired",
            False,
        ),
        (28, "000", "curl: (28) Operation timed out", False),
        (58, "000", "curl: (58) unable to set private key file", False),
    ],
)
def test_retired_certificate_requires_explicit_rejection_and_active_control(
    code: int,
    status: str,
    detail: str,
    accepted: bool,
) -> None:
    lifecycle = _module()
    run = lifecycle.SparkLifecycle.__new__(lifecycle.SparkLifecycle)
    run._psql = lambda _query: [["2"]]
    probes = []

    def probe(serial, root, *, retired):
        probes.append((serial, root, retired))
        return subprocess.CompletedProcess(
            [],
            code if retired else 0,
            status if retired else "404",
            detail if retired else "",
        )

    run._certificate_probe = probe
    assert (
        run._old_certificate_rejected("123456789012345678", "987654321098765432")
        is accepted
    )
    assert probes == [
        (
            "987654321098765432",
            lifecycle.AGENT_DATA / "credentials/generation-00000000000000000002",
            False,
        ),
        ("123456789012345678", lifecycle.AGENT_DATA / "credentials", True),
    ]


@pytest.mark.parametrize("status", ["000", "401", "403", "500"])
def test_retired_certificate_probe_cannot_pass_without_current_identity(
    status: str,
) -> None:
    lifecycle = _module()
    run = lifecycle.SparkLifecycle.__new__(lifecycle.SparkLifecycle)
    run._psql = lambda _query: [["2"]]

    def probe(_serial, _root, *, retired):
        assert not retired, "must not count rejection without a positive control"
        return subprocess.CompletedProcess([], 0, status, "")

    run._certificate_probe = probe
    with pytest.raises(lifecycle.LifecycleError):
        run._old_certificate_rejected("123456789012345678", "987654321098765432")


@pytest.mark.parametrize("foreign_host", [False, True])
def test_lost_start_probe_binds_produced_placement_to_owned_interface(
    tmp_path: Path,
    foreign_host: bool,
) -> None:
    from vonk_agent_protocol import CompiledExecutionPlan, RecipeStartPayload
    from vonk_control.recipe_start_payloads import (
        RecipeStartPlacement,
        build_recipe_start_payload,
    )

    lifecycle = _module()
    address = "172.31.42.1"
    identifier = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    node_id = "spk_" + "b" * 32
    fixture = json.loads(
        (
            Path(__file__).parent.parent
            / "control/tests/fixtures/compiled_workload_v2.json"
        ).read_text(encoding="utf-8")
    )
    payload = RecipeStartPayload.model_validate(
        build_recipe_start_payload(
            run_id=identifier,
            installation_id=identifier,
            recipe_revision_id=identifier,
            mapping_id=identifier,
            run_generation=1,
            plan_digest="c" * 64,
            placement=RecipeStartPlacement(
                node_id, 0, "entrypoint", 8000, 80_000_000, 0, "unified", None
            ),
            compiled_endpoint_address=address,
            world_size=1,
            compiled_execution_plan=CompiledExecutionPlan.model_validate(fixture),
            master_address=None,
            master_port=None,
        )
    )
    placement = payload.compiled_execution_plan.runtime.placement
    endpoint = f"http://{'172.31.43.1' if foreign_host else placement.endpoint_address}:{placement.port}"
    run = lifecycle.SparkLifecycle.__new__(lifecycle.SparkLifecycle)
    run.bundle = tmp_path
    run.temporary_root = tmp_path
    run.arguments = SimpleNamespace(output=tmp_path / "report.json")
    run.synthetic_fabric_octet = 42
    run.synthetic_interfaces = ["vmgt42"]
    run._lost_start_placement = None
    run.synthetic_canary_fixture = SimpleNamespace(serving_check={}, slug="canary")
    commands = []

    def command(argv, **_kwargs):
        commands.append(argv)
        assert argv == ["/usr/sbin/ip", "-j", "-4", "address", "show", "dev", "vmgt42"]
        return subprocess.CompletedProcess(
            argv,
            0,
            json.dumps(
                [
                    {
                        "ifname": "vmgt42",
                        "addr_info": [{"family": "inet", "local": address}],
                    }
                ]
            ),
            "",
        )

    run._run_command = command
    run._run_canonical_inference = lambda *_args, **_kwargs: "verified-response"
    run._observe_start_topology(
        endpoint,
        str(placement.endpoint_address),
        str(placement.port),
        identifier,
        identifier,
        node_id,
        identifier,
    )
    if foreign_host:
        with pytest.raises(lifecycle.LifecycleError):
            run._direct_canary_inference(endpoint)
    else:
        assert run._direct_canary_inference(endpoint) == "verified-response"
    evidence = json.loads((tmp_path / "failed-start-topology.json").read_text())
    assert evidence["accepted_address"] == address
    assert evidence["interface_addresses"] == [address]
    assert evidence["receipt_address"] == ("172.31.43.1" if foreign_host else address)
    assert len(commands) == 1
