import logging

import pytest
import vonk_control.logging as control_logging
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from vonk_control.logging import DatabaseJobLogStore, log_event, redact_text
from vonk_control.models import Base, JobLogEntry


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


def test_job_log_store_is_postgres_backed_content_addressed_and_sanitized() -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine, tables=[JobLogEntry.__table__])
    store = DatabaseJobLogStore(sessionmaker(engine))
    job_id = "00000000-0000-4000-8000-000000000001"
    digest = store.save(job_id, b"started\nAuthorization: Bearer no\nfinished\n")
    content = store.read(job_id, digest)
    assert b"started" in content and b"finished" in content
    assert b"Bearer no" not in content and b"<redacted>" in content
    assert store.list(job_id) == (digest,)


def test_filesystem_job_log_store_is_not_available() -> None:
    assert not hasattr(control_logging, "JobLogStore")


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
def test_download_diagnostics_preserve_source_but_strip_url_credentials(url: str) -> None:
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
        {"detail": "Read failed: https://cdn.example/model?Signature=signed-download-secret"}
    )
    assert "signed-download-secret" not in str(safe)
    assert "https://cdn.example/model" in str(safe)
