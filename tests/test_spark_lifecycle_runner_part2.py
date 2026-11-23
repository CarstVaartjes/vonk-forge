from __future__ import annotations

import ast
import base64
import functools
import importlib.util
import json
import os
import subprocess
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
from typing import Any

import pytest
from spark_lifecycle_runner_support import _module
from vonk_agent_protocol import LifecycleState


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
    operation = _failed_profile_application("runtime.preflight.retry_exhausted")
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


@pytest.mark.parametrize("state", [LifecycleState.RUNNING, LifecycleState.QUEUED])
def test_recipe_download_timeout_preserves_durable_progress(monkeypatch, state) -> None:
    """A busy wait with recovery actions must report evidence, not TypeError."""
    pytest.importorskip(
        "fastapi", reason="Controller contract tests run in the control suite"
    )
    lifecycle = _module()
    from vonk_agent_protocol import (
        ArtifactLifecycleCode,
        OperationProgress,
        ProgressPhase,
    )
    from vonk_control.operations.contract import (
        AvailabilityOperationFailure,
        AvailabilityRecoveryAction,
    )
    from vonk_control.recipe_availability_intent import RecipeSelectorIntent
    from vonk_control.recipe_image_availability_api import (
        RecipeImageAvailabilityAction,
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
            peer_addresses=(),
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


def test_artifact_job_stub_writes_its_result_or_fails_with_stderr(
    tmp_path: Path,
) -> None:
    """The stub the job canary installs, run with the flags the job harness passes."""
    import sys

    from tests.acceptance import artifact_job_canary as job

    script = tmp_path / job.JOB_SCRIPT_NAME
    script.write_bytes(job.JOB_SCRIPT)
    outputs = tmp_path / "outputs"
    outputs.mkdir()

    def run(exit_code: int) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, script, "--exit-code", str(exit_code)]
            + ["--output-dir", str(outputs)],
            capture_output=True,
            text=True,
            check=False,
        )

    succeeded = run(0)
    assert succeeded.returncode == 0
    assert [path.name for path in outputs.iterdir()] == [job.RESULT_NAME]
    assert (outputs / job.RESULT_NAME).read_bytes() == job.RESULT_BYTES
    (outputs / job.RESULT_NAME).unlink()
    failed = run(job.FAILURE_EXIT_CODE)
    assert failed.returncode == job.FAILURE_EXIT_CODE
    assert job.FAILURE_MARKER in failed.stderr
    assert list(outputs.iterdir()) == []


def test_artifact_job_recipe_is_imported_by_the_controller_catalog(
    tmp_path: Path, monkeypatch
) -> None:
    """The derived job Recipe passes the Controller's own catalog sync and policy."""
    pytest.importorskip("vonk_control", reason="requires the control environment")
    library = os.environ.get("VONK_RECIPE_LIBRARY_ROOT")
    if not library:
        pytest.skip("set VONK_RECIPE_LIBRARY_ROOT to the canonical recipe library")
    monkeypatch.setenv(
        "VONK_SYNTHETIC_CANARY_INDEX",
        "tests/fixtures/canonical-synthetic-canary/index.json",
    )
    from datetime import UTC, datetime

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from vonk_control.auth import TokenCodec
    from vonk_control.catalog.service import CatalogService
    from vonk_control.catalog.sync import ManagedRecipeCatalogSyncService
    from vonk_control.compiled_artifact_contract import compile_artifact_contract
    from vonk_control.harnesses.canonical import validate_runtime_policy
    from vonk_control.models import Base
    from vonk_control.source_bundles import SourceBundleStore
    from vonk_forge_contracts import RecipeDefinition

    lifecycle = _module()
    original = lifecycle._canonical_canary_fixture(Path(library))
    fixture = lifecycle._artifact_job_fixture(original)
    original_source = json.loads(original.index_bytes)["recipes"][0][
        "source_bundle_sha256"
    ]
    editorial = lifecycle._editorial_successor(original)
    assert (
        json.loads(editorial.index_bytes)["recipes"][0]["source_bundle_sha256"]
        == original_source
    )
    recipe = RecipeDefinition.model_validate(fixture.recipe)
    validate_runtime_policy(recipe)
    contract = compile_artifact_contract(recipe, "artifact-job")
    assert [parameter.name for parameter in contract.parameters] == ["exit_code"]

    specification = importlib.util.spec_from_file_location(
        "spark_canary_catalog_import", lifecycle.CANARY_CATALOG_IMPORT
    )
    assert specification is not None and specification.loader is not None
    program = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(program)
    index = json.loads(fixture.index_bytes)
    digest = index["recipes"][0]["package"]["sha256"]
    archive = program._cache_package(
        tmp_path / "packages", fixture.package_bytes, digest
    )
    engine = create_engine(f"sqlite:///{tmp_path / 'catalog.sqlite'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    clock = lambda: datetime(2026, 10, 15, tzinfo=UTC)
    reader = program.FixtureReader(index, {digest: archive})
    # Copying the original context digest after adding the job used to make
    # candidate acceptance fail at package ingress. Editorial changes reuse it.
    [item] = reader.snapshot.items
    assert item.source_bundle_sha256 == index["recipes"][0]["source_bundle_sha256"]
    assert item.source_bundle_sha256 != original_source
    assert reader.fetch(item.uri).source_bundle_sha256 == item.source_bundle_sha256
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
        request_key="eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeef",
        snapshot=reader.snapshot,
    )

    assert (view.state, view.commit, view.imported_count, view.problems) == (
        "current",
        fixture.source_commit,
        1,
        (),
    )


def test_artifact_job_canary_speaks_the_controller_contract() -> None:
    """Every request the job canary sends, and every answer it reads, is the contract's.

    A scripted Controller answers with documents its own OpenAPI accepts; the
    canary must drive two jobs to success and one to failure with evidence.
    """
    import hashlib
    import re
    import uuid

    from scripts.development_slice_client import Client
    from tests.acceptance import artifact_job_canary as job
    from tests.acceptance.controller_contract import ControllerContract

    # The operator CLI's copy of the Controller contract.
    openapi = (
        Path(__file__).parents[1] / "src/cluster_profiles/schemas/control-openapi.json"
    )
    if not openapi.is_file():
        pytest.skip("generate the CLI contract with generate-control-clients")
    contract = ControllerContract(json.loads(openapi.read_text()), label="source")
    run_id = str(uuid.uuid4())
    jobs: dict[str, dict[str, object]] = {}
    result = {
        "name": job.RESULT_NAME,
        "media_type": "text/plain",
        "size_bytes": len(job.RESULT_BYTES),
        "sha256": hashlib.sha256(job.RESULT_BYTES).hexdigest(),
    }
    stamp = "2026-10-15T20:00:00Z"
    tail = {"truncated": False, "dropped_bytes": 0, "dropped_lines": 0}
    request_ids: list[str] = []

    def answer(
        method: str, target: str, body: bytes | None
    ) -> tuple[int, dict[str, object] | bytes]:
        path, _, query = target.partition("?")
        if method == "POST" and path.endswith("/artifact-jobs"):
            create = json.loads(body or b"{}")
            job_id = str(uuid.uuid4())
            jobs[job_id] = {
                "id": job_id,
                "run_id": run_id,
                "interface": "artifact-job",
                "state": None,
                "preparation": "draft",
                "contract_sha256": "a" * 64,
                "compiled_contract": None,
                "input_manifest_sha256": "b" * 64,
                "input_total_bytes": 0,
                "input_declarations": None,
                "input_files": [],
                "output_limits": create["output_limits"],
                "output_files": [],
                "timeout_seconds": create["timeout_seconds"],
                "created_at": stamp,
                "updated_at": stamp,
                "exit_code": create["parameters"]["exit_code"],
            }
            return 201, jobs[job_id]
        identifier = path.split("/")[3]
        if path.endswith("/finalize"):
            jobs[identifier]["preparation"] = "ready"
        elif path.endswith("/submit"):
            failing = jobs[identifier]["exit_code"] != 0
            jobs[identifier].update(
                preparation=None,
                operation_id=identifier,
                state="failed" if failing else "succeeded",
                output_files=[] if failing else [result],
            )
            return 202, jobs[identifier]
        elif path.startswith("/api/operations/") and path.endswith("/evidence"):
            assert query == "attempt=1"
            return 200, {
                "schema_version": 2,
                "context": {
                    "operation_id": identifier,
                    "attempt": 1,
                    "kind": "recipe.job.run.v1",
                    "node_ids": [],
                    "updated_at": stamp,
                    "source": "agent",
                },
                "collected_at": stamp,
                "summary": "job adapter exited unsuccessfully",
                "error_code": "artifact_process_failed",
                "diagnostics": {
                    "schema_version": 1,
                    "collected_at": stamp,
                    "phase": "recipe.job.run.v1",
                    "category": "runtime",
                    "stdout": {"text": "", **tail},
                    "stderr": {"text": job.FAILURE_MARKER + "\n", **tail},
                    "versions": [],
                    "sandbox": [],
                    "storage": [],
                    "preflight": [],
                    "collector_errors": [],
                },
                "collector_errors": [],
            }
        elif "/results/" in path:
            return 200, job.RESULT_BYTES
        return 200, jobs[identifier]

    def transport(
        method: str,
        path: str,
        body: bytes | None,
        headers: dict[str, str],
        _timeout: float,
    ) -> tuple[int, bytes]:
        contract.check(method, path, body)
        if path.endswith(("/artifact-jobs", "/submit")):
            request_ids.append(headers["X-Request-ID"])
        status, document = answer(method, path, body)
        if isinstance(document, bytes):
            return status, document
        view = {key: value for key, value in document.items() if key != "exit_code"}
        contract.check_json_response(method, path, view, status=status)
        return status, json.dumps(view).encode()

    control = Client("https://controller", "token", timeout=5, transport=transport)
    job.run_artifact_jobs(
        control,
        run_id=run_id,
        request_key=lambda stage: str(uuid.uuid5(uuid.NAMESPACE_URL, stage)),
        container_names=set,
    )

    exit_codes = sorted(int(str(item["exit_code"])) for item in jobs.values())
    assert exit_codes == [0, 0, job.FAILURE_EXIT_CODE]
    assert len(set(request_ids)) == 6
    assert all(re.fullmatch(r"[0-9a-f-]{36}", value) for value in request_ids)


def test_preview_diagnostic_names_assignment_level_admission_blockers() -> None:
    pytest.importorskip(
        "fastapi", reason="Controller contract tests run in the control suite"
    )
    from vonk_control.fleet_profile_contract import FleetProfileAdmissionDecision

    lifecycle = _module()

    def decision(assignment_id: str, *, blocked: bool) -> dict[str, object]:
        blockers = (
            [
                {
                    "code": "memory.insufficient",
                    "detail": "not enough memory",
                    "severity": "blocker",
                    "scope": "node",
                    "node_ids": ["spk_" + "b" * 32],
                }
            ]
            if blocked
            else []
        )
        return FleetProfileAdmissionDecision.model_validate(
            {
                "assignment_id": assignment_id,
                "alias": None,
                "allowed": not blocked,
                "blockers": blockers,
                "requirements": [],
                "effective_settings": None,
                "stops": [],
                "stop_before_prepare": False,
                "stop_before_transfer": False,
            }
        ).model_dump(mode="json")

    blocked_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    allowed = [
        decision(f"bbbbbbbb-bbbb-4bbb-8bbb-{index:012d}", blocked=False)
        for index in range(9)
    ]
    preview = {
        "allowed": False,
        "summary": {"blockers": 1},
        "reasons": [{"code": "profile.interruption_expected", "severity": "warning"}],
        "admission_decisions": [*allowed, decision(blocked_id, blocked=True)],
    }
    admission = json.loads(lifecycle.SparkLifecycle._preview_diagnostic(preview))[
        "admission"
    ]
    assert [item["assignment_id"] for item in admission] == [blocked_id]
    assert admission[0]["blockers"][0]["code"] == "memory.insufficient"
    assert admission[0]["blockers"][0]["detail"] == "not enough memory"
    assert admission[0]["blockers"][0]["node_ids"] == ["spk_" + "b" * 32]


NODE = "spk_" + "1" * 32


def _canary_lane_under_withdrawal(
    monkeypatch, previews: list[dict[str, object]], *, parked_loads: int = 0
):
    """A lane whose Controller withdraws the canary until a preview is admitted.

    ``previews`` are the profile previews the Controller answers in order. The
    lane stops at the profile load, the first step after the re-offered serve.
    """
    lifecycle = _module()
    from vonk_control.library import contract

    sha = "a" * 64
    fixture = lifecycle.CanonicalCanaryFixture(
        index_path=Path("index.json"),
        index_bytes=b"{}",
        package_path=PurePosixPath("package.tar.gz"),
        package_bytes=b"",
        source_commit="a" * 40,
        publisher="vonk-forge-test",
        slug="canonical-synthetic-canary",
        recipe_content_sha256=sha,
        model_content_sha256="c" * 64,
        role="entrypoint",
        serving_check={},
        recipe={},
    )
    monkeypatch.setattr(
        contract,
        "LibraryRecipeIdentity",
        SimpleNamespace(
            model_validate_json=lambda _value: SimpleNamespace(
                recipe_revision_id="revision",
                publisher=fixture.publisher,
                slug=fixture.slug,
                content_sha256=sha,
            )
        ),
    )
    monkeypatch.setattr(lifecycle, "_canonical_recipe_matches", lambda *_args: True)
    imported: list[str] = []
    downloads: list[str] = []
    remaining = list(previews)

    class Control:
        def request(self, method, path, body=None, allowed=None):
            if path == "/api/catalog/managed-recipes/sync-status":
                return 200, {
                    "trigger": "automatic",
                    "state": "current",
                    "commit": "c" * 40,
                    "withdrawn_count": 2,
                }
            if (method, path) == (
                "GET",
                f"/api/recipe/{fixture.publisher}/{fixture.slug}",
            ):
                return 200, {
                    "identity": {},
                    "selector": f"{fixture.publisher}/{fixture.slug}",
                    "document": {},
                    "model_selectors": ["model"],
                }
            if method == "PUT":
                return 200, {"revision": 1}
            assert (method, path) == ("POST", "/api/profile/1/preview")
            return 200, remaining.pop(0)

    lane = lifecycle.SparkLifecycle.__new__(lifecycle.SparkLifecycle)
    lane.control = Control()
    lane.browser = object()
    lane.synthetic_canary_fixture = fixture
    lane._import_canary_catalog = lambda _fixture, key: (
        imported.append(key)
        or {
            "state": "current",
            "commit": fixture.source_commit,
            "problems": [],
            "total_count": 1,
            "processed_count": 1,
            "imported_count": 1,
            "unchanged_count": 0,
        }
    )
    lane._fleet_snapshot = lambda: {"nodes": [{"id": NODE}]}
    lane._request_recipe_download = lambda _selector, *, request_key: (
        downloads.append(request_key) or {}
    )
    lane._await_recipe_download = lambda *_args, **_kwargs: {
        "result": {
            "recipe_content_sha256": sha,
            "model_content_digests": [fixture.model_content_sha256],
            "image_digest": "sha256:" + "d" * 64,
            "oci_archive_sha256": "e" * 64,
            "image_bytes": 1,
        }
    }

    lane.loads = loads = []
    lane.awaited = awaited = []

    def load(_preview, *, request_key):
        loads.append(request_key)
        if len(loads) <= parked_loads:
            raise lifecycle.LifecycleError("the profile load parked and timed out")
        return {}

    def await_load(*_args, **_kwargs):
        awaited.append(True)
        raise lifecycle.LifecycleError("reached the load await")

    lane._load_canary_profile = load
    lane._await_profile_application = await_load
    lane._installation_failure = lambda stage, error: lifecycle.LifecycleError(
        f"{stage}: {error}"
    )
    return lifecycle, lane, imported, downloads


_WITHDRAWN_PREVIEW = {
    "allowed": False,
    "reasons": [{"code": "profile.recipe_unavailable", "severity": "error"}],
    "preparations": [],
}


_ADMITTED_PREVIEW = {
    "allowed": True,
    "plan_digest": "f" * 64,
    "scope": {"node_ids": [NODE], "idle_node_ids": []},
    "preparations": [
        {
            "preparation": {
                "model": {
                    "model_content_sha256": "c" * 64,
                    "artifact_set_sha256": "1" * 64,
                    "artifact_set_bytes": 1,
                },
                "runtime_image": {
                    "image_digest": "sha256:" + "d" * 64,
                    "oci_layout_sha256": "2" * 64,
                    "image_bytes": 1,
                },
            }
        }
    ],
}


def test_a_canary_the_library_withdrew_is_offered_again_and_served(
    monkeypatch,
) -> None:
    lifecycle, lane, imported, downloads = _canary_lane_under_withdrawal(
        monkeypatch, [_WITHDRAWN_PREVIEW, _ADMITTED_PREVIEW]
    )

    with pytest.raises(lifecycle.LifecycleError, match="reached the load await"):
        lane._run_synthetic_canary(NODE, carry=lambda: None)

    # One re-import, and the second attempt downloads under its own request key.
    assert len(imported) == 2 and imported[0] != imported[1]
    assert len(downloads) == 2 and downloads[0] != downloads[1]
    assert len(lane.loads) == 1


def test_a_canary_withdrawn_between_preview_and_load_is_offered_again(
    monkeypatch,
) -> None:
    """The Controller previews again at load time: the retry covers the load too."""
    lifecycle, lane, imported, downloads = _canary_lane_under_withdrawal(
        monkeypatch, [_ADMITTED_PREVIEW, _ADMITTED_PREVIEW], parked_loads=1
    )

    with pytest.raises(lifecycle.LifecycleError, match="reached the load await"):
        lane._run_synthetic_canary(NODE, carry=lambda: None)

    assert len(imported) == 2 and len(downloads) == 2
    # The second load has its own request key, and only it was awaited.
    assert len(lane.loads) == 2 and lane.loads[0] != lane.loads[1]
    assert lane.awaited == [True]


def test_a_canary_withdrawn_twice_fails_with_the_catalog_sync_status(
    monkeypatch,
) -> None:
    lifecycle, lane, imported, _downloads = _canary_lane_under_withdrawal(
        monkeypatch, [_WITHDRAWN_PREVIEW, _WITHDRAWN_PREVIEW]
    )

    with pytest.raises(lifecycle.LifecycleError) as raised:
        lane._run_synthetic_canary(NODE)

    message = str(raised.value)
    assert "withdrew the fixture again" in message
    assert "profile.recipe_unavailable" in message
    assert '"trigger": "automatic"' in message and '"withdrawn_count": 2' in message
    assert len(imported) == 2


def test_an_unreadable_sync_status_never_replaces_the_canary_failure(
    monkeypatch,
) -> None:
    lifecycle, lane, _imported, _downloads = _canary_lane_under_withdrawal(
        monkeypatch, [_WITHDRAWN_PREVIEW]
    )
    request = lane.control.request

    def without_sync_status(method, path, *args, **kwargs):
        if path == "/api/catalog/managed-recipes/sync-status":
            raise AssertionError((method, path))
        return request(method, path, *args, **kwargs)

    lane.control.request = without_sync_status

    with pytest.raises(lifecycle.LifecycleError) as raised:
        lane._run_synthetic_canary(NODE, carry=lambda: None)

    message = str(raised.value)
    assert "profile.recipe_unavailable" in message
    assert "catalog sync status unknown" in message


def _calls(node, name: str) -> bool:
    import ast

    return any(
        isinstance(call, ast.Call) and getattr(call.func, "attr", None) == name
        for call in ast.walk(node)
    )


def _serves_without_admission(tree) -> list[str]:
    """Serves offered once more that do not reach the load and its await.

    The Controller previews again when it loads, so a serve that ends at the
    preview leaves a withdrawal between preview and load outside the retry.
    """
    import ast

    functions = {
        node.name: node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    }
    missing: list[str] = []
    for call in ast.walk(tree):
        if not (
            isinstance(call, ast.Call)
            and getattr(call.func, "attr", None) == "_offered_once_more"
            and call.args
        ):
            continue
        names = {
            node.attr if isinstance(node, ast.Attribute) else node.id
            for node in ast.walk(call.args[0])
            if isinstance(node, ast.Attribute | ast.Name)
        }
        if not any(
            name in functions
            and _calls(functions[name], "_load_canary_profile")
            and _calls(functions[name], "_await_profile_application")
            for name in names
        ):
            missing.append(f"line {call.lineno}")
    return missing


def _unguarded_fixture_imports(node, enclosing: tuple = ()) -> list[str]:
    import ast

    found: list[str] = []
    for child in ast.iter_child_nodes(node):
        if not isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
            found += _unguarded_fixture_imports(child, enclosing)
            continue
        functions = (*enclosing, child)
        if (
            child.name != "_import_canary_catalog"
            and child.name != "_import_candidate_library"
            and _calls(child, "_import_canary_catalog")
            and not any(_calls(f, "_offered_once_more") for f in functions)
        ):
            found.append(child.name)
        found += _unguarded_fixture_imports(child, functions)
    return found


def test_every_fixture_import_in_the_lane_code_is_offered_once_more() -> None:
    """A fixture import that is then served must go through _offered_once_more.

    The live recipe library can be republished while a lane runs, which
    withdraws the fixture by design. Each function that imports a fixture must
    sit inside, or be, a function that calls ``_offered_once_more``, and the
    serve it wraps must reach the profile load and its await. The
    candidate library is only imported: the carry that follows serves the
    workload that already runs.
    """
    import ast

    root = Path(__file__).parent / "acceptance"
    unguarded: list[str] = []
    for name in ("test_spark_lifecycle.py", "spark_upgrade_carry.py"):
        tree = ast.parse((root / name).read_text(encoding="utf-8"))
        unguarded += [f"{name}:{f}" for f in _unguarded_fixture_imports(tree)]
        unguarded += [
            f"{name}: no load in the serve at {w}"
            for w in _serves_without_admission(tree)
        ]
    assert unguarded == []


def _blocked(code: str) -> dict[str, Any]:
    return {
        "allowed": False,
        "admission_decisions": [{"allowed": False, "blockers": [{"code": code}]}],
    }


_SWAP = "run-switch.resource.insufficient_swap"


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def sleep(self, seconds: float) -> None:
        self.now += seconds


@functools.cache
def _lane():
    return _module()


def _await(
    responses: list[dict[str, Any]], clock: _Clock, deadline_seconds: float = 270.0
) -> dict[str, Any]:
    queue = iter(responses)
    return _lane().await_admitted_preview(
        lambda: next(queue),
        label="preview",
        describe=lambda preview: json.dumps(preview, sort_keys=True),
        clock=lambda: clock.now,
        sleep=clock.sleep,
        deadline_seconds=deadline_seconds,
    )


def test_preview_waits_on_typed_waits_then_returns_the_admitted_answer() -> None:
    clock = _Clock()
    admitted = {"allowed": True}

    assert _await([_blocked(_SWAP), _blocked(_SWAP), admitted], clock) is admitted
    assert clock.now == 6.0


def test_preview_refusal_fails_at_once_without_waiting() -> None:
    clock = _Clock()

    with pytest.raises(_lane().LifecycleError, match="not admitted"):
        _await([_blocked(_SWAP.replace("swap", "other")), {"allowed": True}], clock)
    assert clock.now == 0.0
    with pytest.raises(_lane().LifecycleError, match="not admitted"):
        _await([{"allowed": False}], clock)
    assert clock.now == 0.0


def test_preview_wait_past_the_deadline_fails_with_the_last_reason() -> None:
    clock = _Clock()

    with pytest.raises(
        _lane().LifecycleError, match="still waiting.*insufficient_swap"
    ):
        _await([_blocked(_SWAP)] * 100, clock, deadline_seconds=60.0)
    assert clock.now <= 60.0


def test_a_wait_beside_an_error_reason_is_a_refusal() -> None:
    clock = _Clock()
    preview = _blocked(_SWAP) | {"reasons": [{"severity": "error", "code": "x"}]}

    with pytest.raises(_lane().LifecycleError, match="not admitted"):
        _await([preview, {"allowed": True}], clock)
    assert clock.now == 0.0


def test_the_raw_profile_preview_request_lives_only_in_the_admitted_preview() -> None:
    """Catches a single-shot preview that fails on a wait the next cycle clears."""
    acceptance = Path(__file__).parent / "acceptance"
    found: list[tuple[str, str]] = []
    for path in sorted(acceptance.glob("*.py")):
        tree = ast.parse(path.read_text())
        for function in ast.walk(tree):
            if not isinstance(function, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            for node in ast.walk(function):
                if (
                    isinstance(node, ast.Constant)
                    and node.value == "/api/profile/1/preview"
                ):
                    found.append((path.name, function.name))
        module_level = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and node.value == "/api/profile/1/preview"
        ]
        assert len(module_level) == len(
            [item for item in found if item[0] == path.name]
        ), f"{path.name}: preview path outside a function"
    assert set(found) == {("test_spark_lifecycle.py", "_admitted_preview")}
