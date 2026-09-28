import logging

import pytest
from vonk_control.logging import (
    configure_controller_logging,
    log_event,
    redact_text,
)


def test_structured_logger_redacts_secrets(caplog) -> None:
    with caplog.at_level(logging.INFO, logger="test-control"):
        log_event(
            logging.getLogger("test-control"),
            "job.failed",
            service="control-worker",
            request_id="request",
            token="secret-value",
            stderr="Authorization: Bearer abc123 password=hunter2",
        )
    assert "secret-value" not in caplog.text
    assert "abc123" not in caplog.text
    assert "hunter2" not in caplog.text
    assert "<redacted>" in caplog.text


def test_controller_api_and_worker_info_events_are_emitted_at_runtime_config(
    capsys,
) -> None:
    configure_controller_logging()
    api_logger = logging.getLogger("vonk_control.api")
    worker_logger = logging.getLogger("vonk-control-worker")
    run_switch_logger = logging.getLogger("vonk-control-run-switch")
    assert api_logger.getEffectiveLevel() == logging.INFO
    assert worker_logger.getEffectiveLevel() == logging.INFO
    assert run_switch_logger.getEffectiveLevel() == logging.INFO

    log_event(api_logger, "api.request_failed", service="controller", request_id="api")
    log_event(
        worker_logger,
        "worker.source_failed",
        service="control-worker",
        source="runs",
        token="must-not-be-logged",
    )
    log_event(
        run_switch_logger,
        "run_switch.final_verification_expired",
        service="control-worker",
        operation_id="operation",
        run_id="run",
        route_state="pending",
    )

    output = capsys.readouterr().err
    assert '"event":"api.request_failed"' in output
    assert '"event":"worker.source_failed"' in output
    assert '"event":"run_switch.final_verification_expired"' in output
    assert "must-not-be-logged" not in output


def test_redaction_truncates_remote_output() -> None:
    value = redact_text("x" * 100_000)
    assert len(value) <= 4096
    assert value.endswith("<truncated>")


@pytest.mark.parametrize(
    "url",
    [
        "https://cdn-lfs.hf.co/model/weights?X-Amz-Signature=secret-signature&Policy=secret-policy",
        "https://cas-bridge.xethub.hf.co/model/weights?opaque-provider-field=secret-signature",
        "https://user:secret-password@example.com/model/weights#secret-fragment",
    ],
)
def test_download_diagnostics_preserve_source_but_strip_url_credentials(
    url: str,
) -> None:
    safe = redact_text(f"download failed ({url}); retry is available")
    assert "secret-" not in safe
    assert "user:" not in safe
    assert "/model/weights" in safe
    assert safe.endswith("); retry is available")


def test_model_access_url_without_credentials_remains_useful() -> None:
    message = "Request access at https://huggingface.co/creator/Model. Then resume."
    assert redact_text(message) == message


def test_persisted_failure_evidence_does_not_retain_signed_download_queries() -> None:
    from vonk_control.operation_contract import sanitize_failure_evidence

    safe = sanitize_failure_evidence(
        {
            "detail": "Read failed: https://cdn.example/model?Signature=signed-download-secret"
        }
    )
    assert "signed-download-secret" not in str(safe)
    assert "https://cdn.example/model" in str(safe)
