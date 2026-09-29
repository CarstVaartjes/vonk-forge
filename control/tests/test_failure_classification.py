import pytest
from vonk_control.failure_classification import (
    error_code,
    is_redownload,
    is_security_failure,
)


@pytest.mark.parametrize(
    "code",
    [
        "401",
        "403",
        "local.identity_expired",
        "agent.identity_mismatch",
        "agent.tombstone_fenced",
        "controller.fleet.enrollment_denied",
        "controller.authentication_required",
        "host_helper.authority_denied",
        "runtime_image.authorization_revoked",
        "distribution.revoked",
        "tuf.signature_invalid",
        "model_cache.credentials_denied",
        # Helper and preflight codes emitted un-dotted.
        "helper_grant_invalid",
        "helper_request_replayed",
        "request_replayed",
        "controller.request_rejected",
        "agent.certificate.rotation.conflict",
    ],
)
def test_security_boundaries_are_terminal(code: str) -> None:
    assert is_security_failure(code)
    assert not is_redownload(code)


@pytest.mark.parametrize(
    "code",
    [
        None,
        "",
        "run-switch.stale_plan",
        "run-switch.plan_blocked",
        "install.plan_stale_or_blocked",
        "runtime_image.transport_failed",
        "runtime_image.digest_mismatch",
        "registry.digest_mismatch",
        "runtime_image.archive_mismatch",
        "an error mentioning signature and identity",
    ],
)
def test_other_failures_are_retried(code: str | None) -> None:
    assert not is_security_failure(code)


def test_downloaded_byte_integrity_failures_request_redownload() -> None:
    assert is_redownload("runtime_image.digest_mismatch")
    assert is_redownload("model_cache.digest_mismatch")
    assert is_redownload("runtime_image.archive_mismatch")
    assert not is_redownload("runtime_image.receipt_invalid")
    assert not is_redownload(None)


def test_error_code_prefers_typed_code_then_dotted_message_prefix() -> None:
    typed = RuntimeError("free text")
    typed.code = "tuf.signature_invalid"  # type: ignore[attr-defined]
    assert error_code(typed) == "tuf.signature_invalid"
    assert error_code(RuntimeError("run-switch.stale_plan: preview moved")) == (
        "run-switch.stale_plan"
    )
    assert error_code(RuntimeError("identity mismatch: prose only")) is None


def test_error_code_keeps_a_wrapped_security_code() -> None:
    try:
        try:
            raise RuntimeError("host_helper.authority_denied: grant rejected")
        except RuntimeError as inner:
            raise ValueError("run-switch.install-start-failed: wrapped") from inner
    except ValueError as outer:
        assert error_code(outer) == "host_helper.authority_denied"
