"""Spark lifecycle runner: identity install failures."""

from __future__ import annotations

import base64
import json
import subprocess
from pathlib import Path, PurePosixPath
from types import SimpleNamespace

import pytest

from tests.spark_lifecycle_runner_support import _module
from vonk_agent_protocol.lifecycle_vocabulary import LifecycleState


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

    # A baseline Controller may still list a retired service name.
    baseline = dict(
        grant,
        service_hostnames=[
            *grant["service_hostnames"],
            "registry.vonk-forge-acceptance.tailnet.example",
        ],
    )
    Control.request = staticmethod(
        lambda method, path, body: (201, {"grant": baseline})
    )
    assert run._create_grant()[0] == grant["id"]

    for invalid in (
        dict(grant, installer_url="https://install.vonkforge.ai/spark"),
        dict(grant, service_hostnames=grant["service_hostnames"][:2]),
    ):
        Control.request = staticmethod(
            lambda method, path, body, invalid=invalid: (201, {"grant": invalid})
        )
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


@pytest.mark.parametrize("observed", ["sha256:expected", "sha256:different"])
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
    from vonk_agent_protocol.wire_model import OperationProgress
    from vonk_control.recipe_image_availability.api import (
        RecipeImageAvailabilityResponse,
        RecipeImageAvailabilityResult,
    )
    from vonk_control.recipe_operations.availability_intent import RecipeSelectorIntent

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


@pytest.mark.parametrize("state", [LifecycleState.RUNNING, LifecycleState.QUEUED])
def test_recipe_download_timeout_preserves_durable_progress(monkeypatch, state) -> None:
    """A busy wait with recovery actions must report evidence, not TypeError."""
    pytest.importorskip(
        "fastapi", reason="Controller contract tests run in the control suite"
    )
    lifecycle = _module()
    from vonk_agent_protocol.agent_words import ProgressPhase
    from vonk_agent_protocol.reason_codes.evidence import ArtifactLifecycleCode
    from vonk_agent_protocol.wire_model import OperationProgress
    from vonk_control.operations.contract import (
        AvailabilityOperationFailure,
        AvailabilityRecoveryAction,
    )
    from vonk_control.recipe_image_availability.api import (
        RecipeImageAvailabilityAction,
        RecipeImageAvailabilityResponse,
    )
    from vonk_control.recipe_operations.availability_intent import RecipeSelectorIntent

    recipe_digest = "a" * 64
    pending = RecipeImageAvailabilityResponse(
        id="11111111-1111-4111-8111-111111111111",
        request_id="22222222-2222-4222-8222-222222222222",
        request=RecipeSelectorIntent(
            selector="acceptance/synthetic-canary", force=False
        ),
        kind="recipe.image.availability.v2",
        state=state,
        attempt=2,
        recipe_revision_id="33333333-3333-4333-8333-333333333333",
        recipe_content_sha256=recipe_digest,
        progress=OperationProgress(phase=ProgressPhase.WAITING, completed_bytes=4),
        failure=AvailabilityOperationFailure(
            code=ArtifactLifecycleCode.REFERENCE_BUSY,
            detail="concurrent reference owner",
            retryable=True,
            recovery_actions=[AvailabilityRecoveryAction.RETRY],
            retry_time="2026-09-10T10:00:09Z",
            retry_after_seconds=5,
        ),
        actions=[
            RecipeImageAvailabilityAction(key=action)
            for action in AvailabilityRecoveryAction
        ],
        next_attempt_at="2026-09-10T10:00:09Z",
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
    ) as failure:
        run._await_recipe_download(
            pending.model_dump(mode="json"),
            fixture=fixture,
            recipe_revision_id=pending.recipe_revision_id,
        )

    message = str(failure.value)
    evidence = json.loads(message.split("did not converge: ", 1)[1])
    assert evidence["state"] == state
    assert evidence["attempt"] == pending.attempt
    assert evidence["progress"]["phase"] == ProgressPhase.WAITING
    assert evidence["progress"]["completed_bytes"] == 4
    assert evidence["failure"]["code"] == ArtifactLifecycleCode.REFERENCE_BUSY
    assert evidence["failure"]["retryable"] is True
    assert evidence["actions"] == [
        {"key": action.value} for action in AvailabilityRecoveryAction
    ]
    assert evidence["next_attempt_at"] == pending.next_attempt_at
    assert evidence["updated_at"] == pending.updated_at


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
    pending["state"] = "queued"
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
