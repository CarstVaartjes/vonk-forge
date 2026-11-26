"""Spark lifecycle runner: bootstrap canary."""

from __future__ import annotations

import base64
import importlib.util
import json
import os
import subprocess
import uuid
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
from typing import TypedDict

import pytest
import yaml
from sqlalchemy import create_engine, insert, text
from sqlalchemy.orm import sessionmaker
from vonk_forge_contracts import CONTRACT_MAJOR

from cluster_profiles.control_client.schema import validate_control_document
from control.tests.index_envelope import index_envelope
from scripts.fleet.canary_package import canary_package_path
from tests.spark_lifecycle_runner_support import ENTRY_POINT, _module
from vonk_agent_protocol.claims import AgentRuntimeIdentity
from vonk_agent_protocol.state_machines import NodeIdentityState
from vonk_control.auth import TokenCodec
from vonk_control.catalog.service import CatalogService
from vonk_control.catalog.sync import ManagedRecipeCatalogSyncService
from vonk_control.fleet_projection.common import FleetSnapshot
from vonk_control.model_primitives import Base
from vonk_control.models.fleet import AgentCertificate, AgentNode
from vonk_control.presence import ManagementAddressPolicy, PresenceError
from vonk_control.source_bundles import SourceBundleStore


class _Observed(TypedDict):
    # Filled in by the `interactive` callback, which the code under test invokes
    # before the test reads any key, so a read never observes a placeholder.
    command: list[str]
    responses: list[tuple[str, str]]
    forbidden_values: list[str]
    environment: dict[str, str]


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
    (bundle / "docker-compose.yaml").write_text(
        "services:\n  control-api:\n    image: ghcr.io/vonk/api@sha256:"
        + "a" * 64
        + "\n    environment:\n      VONK_DEPLOYMENT_MODE: production\n"
        + "  caddy:\n    image: caddy:acceptance\n    networks: [ingress]\n    ports:\n"
        + "      - target: 8443\n        published: 8443\n"
        + "    volumes:\n      - caddy-data:/data\n"
        + "networks:\n  ingress: {}\n  cluster-egress: {}\n"
    )

    compose_path = bundle / "docker-compose.yaml"
    compose_document = yaml.safe_load(compose_path.read_text())
    published = yaml.safe_load(
        (ENTRY_POINT.parents[2] / "deploy/compose/compose.yaml").read_text()
    )
    compose_document["services"]["caddy"]["entrypoint"] = published["services"][
        "caddy"
    ]["entrypoint"]
    compose_path.write_text(yaml.safe_dump(compose_document))

    lifecycle._configure_acceptance_renewal(
        bundle,
        lifetime_seconds=lifecycle.CERTIFICATE_LIFETIME_SECONDS,
        agent_source_address="172.31.42.1",
    )

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
    # A historical signed bundle's wrapper waited for the staged entrypoint and
    # discarded command arguments. Its native entrypoint therefore selected the
    # packaged configuration, not the fixture's source-bound configuration.
    # Exercise that exact countercase.
    historical = [
        "/bin/sh",
        "-c",
        (
            "attempts=0; until [ -f /run/vonk-runtime-assets/caddy/entrypoint.sh ]; "
            'do attempts=$$((attempts + 1)); if [ "$$attempts" -ge 120 ]; then '
            "exit 1; fi; sleep 1; done; "
            "exec /bin/sh /run/vonk-runtime-assets/caddy/entrypoint.sh"
        ),
    ]
    old_wrapper = [
        argument.replace("$$", "$").replace(
            "/run/vonk-runtime-assets/caddy/entrypoint.sh", str(native)
        )
        for argument in historical
    ]
    environment = {
        "PATH": str(executable.parent) + os.pathsep + os.defpath,
        "VONK_CONTROL_HOSTNAME": "spark.acceptance.invalid",
        "CAPTURE_ARGUMENTS": str(captured),
    }
    subprocess.run(
        [*old_wrapper, *service["command"]], check=True, timeout=30, env=environment
    )
    old_selected = captured.read_text().splitlines()
    assert old_selected != service["command"][1:]
    assert (
        old_selected[old_selected.index("--config") + 1]
        == "/run/vonk-runtime-assets/caddy/Caddyfile"
    )
    # Its fixture adaptation preserves the entire release's wait script and
    # applies only the reviewed final-argv correction.
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
        timeout=30,
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
        timeout=30,
        env={
            "PATH": str(executable.parent) + os.pathsep + os.defpath,
            "VONK_CONTROL_HOSTNAME": "spark.acceptance.invalid",
            "CAPTURE_ARGUMENTS": str(captured),
        },
    )
    assert captured.read_text().splitlines() == service["command"][1:]
    selected = captured.read_text().splitlines()
    assert selected[selected.index("--config") + 1] == "/etc/caddy/Caddyfile"

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

    expected = FleetSnapshot.model_validate_json(json.dumps(expected_payload))
    assert run._fleet_snapshot() == expected.model_dump(mode="json")


@pytest.mark.parametrize("schema", ["candidate", "baseline"])
@pytest.mark.parametrize("initial_runtime_matches", [True, False])
def test_agent_identity_wait_requires_current_identity_and_builder_capability(
    monkeypatch, initial_runtime_matches: bool, schema: str
) -> None:
    """Catches an identity read that depends on a table one side of the upgrade
    carry lacks: the baseline Controller keeps contact facts on agent_nodes."""

    lifecycle = _module()
    node_id = "spk_" + "1" * 32
    self_test = {
        "semantic_version": "1.2.3",
        "architecture": "linux-arm64",
        "build_digest": "sha256:" + "a" * 64,
        "binary_digest": "b" * 64,
    }
    runtime_identity = AgentRuntimeIdentity.model_validate_json(json.dumps(self_test))
    responses = iter(
        [
            {
                "id": node_id,
                "display_name": "Spark",
                "agent_runtime": (
                    runtime_identity
                    if initial_runtime_matches
                    else runtime_identity.model_copy(update={"binary_digest": "c" * 64})
                ).model_dump(mode="json"),
                "connection": {
                    "agent_state": "active",
                    "online_state": "online",
                    "certificate_state": "valid",
                },
                "inventory": {
                    "freshness": "fresh",
                    "capabilities": []
                    if initial_runtime_matches
                    else ["recipe.build.v1"],
                },
            },
            {
                "id": node_id,
                "display_name": "Spark",
                "agent_runtime": runtime_identity.model_dump(mode="json"),
                "connection": {
                    "agent_state": "active",
                    "online_state": "online",
                    "certificate_state": "valid",
                },
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
    monkeypatch.setattr(lifecycle.time, "sleep", lambda _seconds: None)
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(
            insert(AgentNode).values(
                node_id=node_id,
                state=NodeIdentityState.ACTIVE,
            )
        )
        connection.execute(
            insert(AgentCertificate).values(
                serial="1234567890",
                node_id=node_id,
                fingerprint="f" * 64,
                not_before=datetime(2026, 10, 1, tzinfo=UTC),
                not_after=datetime(2026, 11, 1, tzinfo=UTC),
            )
        )
        if schema == "baseline":
            # The baseline Controller: contact facts on agent_nodes, no
            # agent_node_contacts table.
            connection.exec_driver_sql("DROP TABLE agent_node_contacts")
            connection.exec_driver_sql(
                "ALTER TABLE agent_nodes ADD COLUMN contact_certificate_serial TEXT"
            )

        def psql(query: str) -> list[list[str]]:
            # Exercise the actual current schema: a canned row concealed the
            # acceptance harness's reads of retired runtime identity columns.
            return [
                [str(value) for value in row] for row in connection.execute(text(query))
            ]

        run._psql = psql
        identity = run._wait_for_agent_identity(package_version="1.2.3", timeout=1)
    engine.dispose()

    assert identity["node_id"] == node_id
    assert identity["package_sha256"] == "d" * 64
    assert identity["serial"] == "1234567890"


def test_synthetic_canary_download_uses_the_current_operator_request_shape() -> None:

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


@pytest.mark.parametrize(
    ("returncode", "failures"), [(0, []), (1, ["Recipes lost: ['a/b']"])]
)
def test_future_vector_read_gates_on_the_installed_controller_verdict(
    tmp_path: Path, returncode: int, failures: list[str]
) -> None:
    """The vector's bytes reach the in-container program; any failure fails the lane."""
    lifecycle = _module()
    commands: list[tuple[list[str], str | None]] = []

    def run_command(command, *, input_text=None, **_kwargs):
        commands.append((list(command), input_text))
        return subprocess.CompletedProcess(
            command,
            returncode,
            stdout="warning\n" + json.dumps({"failures": failures}) + "\n",
            stderr="",
        )

    run = lifecycle.SparkLifecycle.__new__(lifecycle.SparkLifecycle)
    run.bundle = tmp_path
    run.project = "vonk-spark-1-arm64"
    run._run_command = run_command

    if failures:
        with pytest.raises(lifecycle.LifecycleError):
            run._read_future_vector(b'{"recipes": []}')
    else:
        assert run._read_future_vector(b'{"recipes": []}') == {"failures": []}
    [(command, input_text)] = commands
    assert command[command.index("exec") :][4:7] == ["control-api", "python", "-c"]
    assert command[-1] == lifecycle.FUTURE_VECTOR_READ.read_text()
    assert input_text == '{"recipes": []}'


def test_canary_catalog_import_applies_the_producer_fixture(tmp_path: Path) -> None:
    """The in-container program imports the real canary through catalog sync."""
    pytest.importorskip("vonk_control", reason="requires the control environment")
    library = os.environ.get("VONK_RECIPE_LIBRARY_ROOT")
    if not library:
        pytest.skip("set VONK_RECIPE_LIBRARY_ROOT to the canonical recipe library")

    specification = importlib.util.spec_from_file_location(
        "spark_canary_catalog_import", _module().CANARY_CATALOG_IMPORT
    )
    assert specification is not None
    assert specification.loader is not None
    program = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(program)
    index_path = Path(library) / "tests/fixtures/canonical-synthetic-canary/index.json"
    index = json.loads(index_path.read_text())
    package = canary_package_path(
        index_path, index["recipes"][0]["package"]["path"]
    ).read_bytes()
    digest = index["recipes"][0]["package"]["sha256"]
    archive = program._cache_package(tmp_path / "packages", package, digest)
    assert archive == tmp_path / "packages" / digest[:2] / f"{digest}.tar.gz"
    engine = create_engine(f"sqlite:///{tmp_path / 'catalog.sqlite'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)

    def clock():
        return datetime(2026, 9, 28, tzinfo=UTC)

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


def _release_index(recipes: list[object]) -> dict[str, object]:
    """A release-shaped library index this Controller's parser accepts."""

    return index_envelope(
        contract_version=f"{CONTRACT_MAJOR}.0.0",
        updated_at="1970-01-01T00:00:00Z",
        source_commit="c" * 40,
        recipes=recipes,
    )


def test_canary_import_refuses_an_index_the_installed_controller_skips() -> None:
    """A skipped canary would sync nothing and look like success; it fails here."""
    pytest.importorskip("vonk_control", reason="requires the control environment")
    specification = importlib.util.spec_from_file_location(
        "canary_import_refusal",
        ENTRY_POINT.with_name("spark_canary_catalog_import.py"),
    )
    assert specification is not None
    assert specification.loader is not None
    program = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(program)
    unreadable = {"document": {"kind": "recipe"}, "package": {}}

    with pytest.raises(ValueError, match="cannot read the canary index"):
        program.FixtureReader(_release_index([unreadable]), {})


def test_canary_catalog_path_calls_the_installed_service_contract(tmp_path):
    """Catches keyword drift in the executable harness, without a recipe checkout.

    The real service runs its acceptance and durable sync path; no stub.
    """

    specification = importlib.util.spec_from_file_location(
        "canary_import_contract",
        ENTRY_POINT.with_name("spark_canary_catalog_import.py"),
    )
    assert specification is not None
    assert specification.loader is not None
    program = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(program)
    engine = create_engine(f"sqlite:///{tmp_path / 'sync.sqlite'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)

    def clock():
        return datetime(2026, 10, 9, tzinfo=UTC)

    reader = program.FixtureReader(_release_index([]), {})
    snapshot = reader.snapshot
    service = ManagedRecipeCatalogSyncService(
        sessions,
        catalog=CatalogService(
            sessions, clock=clock, cursors=TokenCodec(b"s" * 32).cursor_codec()
        ),
        reader=reader,
        clock=clock,
    )

    installed = service
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
    assert first.completed_at is not None
    assert not first.problems
    assert replay.id == first.id
    assert fresh.id != first.id
    assert fresh.completed_at is not None
    assert not fresh.problems


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
