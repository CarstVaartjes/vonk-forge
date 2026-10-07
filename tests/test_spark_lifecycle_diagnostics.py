"""Failure evidence survives empty exceptions, observation outages and cleanup."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.test_spark_lifecycle_runner import _module


def _arguments(tmp_path: Path):
    return SimpleNamespace(
        channel="dev",
        version="1.2.3",
        source_sha="a" * 40,
        generation="b" * 64,
        run_id=42,
        platform="linux-arm64",
        output=tmp_path / "report.json",
    )


@pytest.mark.parametrize(
    "fault", ["blank", "observation", "cleanup", "both", "delayed", "unavailable"]
)
def test_failure_report_survives_every_failure_boundary(
    fault: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _module()
    arguments = _arguments(tmp_path)
    monkeypatch.setattr(module, "check_publication_graph", lambda _arguments: {})
    monkeypatch.setattr(module, "validate_lifecycle", lambda *_args, **_kwargs: None)
    clock = [0.0]
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(
        module.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds)
    )
    run = module.SparkLifecycle.__new__(module.SparkLifecycle)
    attempts: list[float] = []
    cleanup_reports: list[dict] = []

    def transport(method, path, _body, _headers, timeout):
        assert method == "GET" and path == "/litellm/health/readiness"
        assert 0 < timeout <= 30
        attempts.append(clock[0])
        return (200 if fault == "delayed" and clock[0] >= 100 else 502), b"{}"

    inference = module.Client(
        "https://gateway.invalid", "test-key", timeout=30, transport=transport
    )

    def observation():
        raise module.SliceError("GET /api/fleet returned HTTP 503")

    run._fleet_snapshot = observation
    run.control = None
    run.browser = None

    class Lifecycle:
        def __enter__(self):
            return self

        def observe(self):
            if fault == "blank":
                run._run_synthetic_canary("spk_" + "1" * 32)
            elif fault == "observation":
                run._await_canary_endpoint("canary", published=False)
            elif fault in {"delayed", "unavailable"}:
                run._await_inference_ready(inference)
            elif fault == "cleanup":
                return {}
            raise AssertionError()

        def __exit__(self, *_error):
            if fault != "cleanup":
                cleanup_reports.append(json.loads(arguments.output.read_text()))
            if fault in {"cleanup", "both"}:
                raise OSError("cleanup disk failure")

    with pytest.raises(module.LifecycleError) as failure:
        module.run_lifecycle(arguments, lifecycle_factory=lambda *_args: Lifecycle())
    report = json.loads(arguments.output.read_text())
    assert report["status"] == "failed"
    assert report["failure"]["cause"]
    assert "\n" not in report["failure"]["cause"]
    assert str(failure.value)
    assert not list(tmp_path.glob(".report.json.*"))
    if fault != "cleanup":
        assert cleanup_reports[0]["status"] == "failed"
        assert cleanup_reports[0]["failure"]["cause"]
    if fault == "both":
        assert "AssertionError" in report["failure"]["cause"]
        assert "cleanup disk failure" in report["failure"]["cause"]
    if fault == "blank":
        assert "phase=synthetic canary/preconditions" in report["failure"]["cause"]
        assert "AssertionError" in report["failure"]["cause"]
    if fault == "observation":
        assert "GET /api/fleet returned HTTP 503" in report["failure"]["cause"]
    if fault == "cleanup":
        assert report["failure"]["phase"] == "cleanup"
    if fault == "delayed":
        assert clock[0] >= 100 and len(attempts) > 1
    if fault == "unavailable":
        assert (
            "GET /litellm/health/readiness returned HTTP 502"
            in report["failure"]["cause"]
        )
        assert clock[0] == module._LITELLM_READINESS_SECONDS
    # A failed attempt leaves no local admission gate: a fresh run succeeds.
    monkeypatch.setattr(Lifecycle, "observe", lambda self: {})
    monkeypatch.setattr(Lifecycle, "__exit__", lambda *_args: None)
    module.run_lifecycle(arguments, lifecycle_factory=lambda *_args: Lifecycle())
    assert json.loads(arguments.output.read_text())["status"] == "passed"


def test_same_line_diagnostics_preserve_type_phase_request_and_chain(monkeypatch):
    module = _module()
    run = module.SparkLifecycle.__new__(module.SparkLifecycle)
    monkeypatch.setenv("VONK_ACCEPTANCE_LITELLM_UPSTREAM_KEY", "secret-value")
    try:
        try:
            raise TimeoutError()
        except TimeoutError as cause:
            raise module.SliceError(
                "POST /v1/chat/completions returned HTTP 502\nsecret-value"
            ) from cause
    except module.SliceError as error:
        rendered = str(run._installation_failure("inference", error))
    assert "installer error: SliceError" in rendered.splitlines()[0]
    assert "phase=inference" in rendered.splitlines()[0]
    assert "HTTP 502" in rendered.splitlines()[0]
    assert "TimeoutError" in rendered.splitlines()[0]
    assert "secret-value" not in rendered


@pytest.mark.parametrize("strip_diagnostics", [False, True])
def test_failed_report_cannot_be_signed_as_release_evidence(
    strip_diagnostics: bool,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import importlib.machinery
    import importlib.util

    module = _module()
    arguments = _arguments(tmp_path)
    module._write_failure_report(arguments, AssertionError(), phase="observation")
    if strip_diagnostics:
        report = json.loads(arguments.output.read_text())
        del report["failure"]
        module._atomic_write(arguments.output, report)
    root = Path(__file__).resolve().parents[1]
    monkeypatch.syspath_prepend(str(root / "scripts"))
    loader = importlib.machinery.SourceFileLoader(
        "acceptance_report_signer", str(root / "scripts/install-release-publication")
    )
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    signer = importlib.util.module_from_spec(spec)
    loader.exec_module(signer)
    monkeypatch.setattr(signer, "_signing_public_key", lambda *_args: b"test-key")
    monkeypatch.setattr(signer, "recompute_publication_graphs", lambda **_kwargs: {})
    arguments.signing_key = tmp_path / "unused-private-key"
    arguments.signing_public_key = tmp_path / "unused-public-key"
    arguments.gate_report = [arguments.output]
    arguments.candidate_release = tmp_path / "unused-candidate"
    arguments.baseline_release = tmp_path / "unused-baseline"
    arguments.object_root = tmp_path
    with pytest.raises(signer.PublicationError, match="behavioral gate report"):
        signer.accept(arguments)


def test_startup_failure_reports_before_cleanup_and_allows_a_fresh_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _module()
    arguments = _arguments(tmp_path)
    monkeypatch.setattr(module, "check_publication_graph", lambda _arguments: {})
    monkeypatch.setattr(module, "validate_lifecycle", lambda *_args, **_kwargs: None)
    run = module.SparkLifecycle.__new__(module.SparkLifecycle)
    run.arguments = arguments
    run._assert_spark_target_is_fresh = lambda: None
    observed: list[dict] = []

    def start():
        raise TimeoutError()

    run._start_controller = start
    run._cleanup = lambda: observed.append(json.loads(arguments.output.read_text()))
    with pytest.raises(module.LifecycleError, match="TimeoutError"):
        module.run_lifecycle(arguments, lifecycle_factory=lambda *_args: run)
    assert observed[0]["status"] == "failed"
    assert observed[0]["failure"]["phase"] == "controller-startup"
    assert "TimeoutError" in observed[0]["failure"]["cause"]
    run._start_controller = lambda: None
    run.observe = dict
    module.run_lifecycle(arguments, lifecycle_factory=lambda *_args: run)
    assert json.loads(arguments.output.read_text())["status"] == "passed"


@pytest.mark.parametrize("failure", ["access", "tls"])
def test_readiness_does_not_retry_security_failures(failure, monkeypatch):
    import ssl

    module = _module()
    attempts = []

    def transport(*_args):
        attempts.append(1)
        if failure == "tls":
            raise ssl.SSLCertVerificationError("untrusted certificate")
        return 403, b"{}"

    client = module.Client(
        "https://gateway.invalid", "test-key", timeout=30, transport=transport
    )
    monkeypatch.setattr(
        module.time, "sleep", lambda _seconds: pytest.fail("security failure retried")
    )
    with pytest.raises(module.SliceError):
        module.SparkLifecycle._await_inference_ready(client)
    assert len(attempts) == 1
    assert client.timeout == 30


def test_noisy_logs_cannot_hide_the_canary_phase_in_final_stderr(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = _module()
    run = module.SparkLifecycle.__new__(module.SparkLifecycle)
    run.bundle = tmp_path
    run._controller_startup_diagnostics = lambda: "noisy logs " * 1_000
    run._compose = lambda *_args: ["diagnostic"]
    run._diagnostic_command = lambda *_args: None

    def fail(_arguments):
        try:
            raise module.SliceError("GET /api/fleet returned HTTP 503")
        except module.SliceError as cause:
            raise run._installation_failure(
                "synthetic canary/route-publication", cause
            ) from cause

    monkeypatch.setattr(module, "_arguments", lambda: SimpleNamespace(command="run"))
    monkeypatch.setattr(module, "run_lifecycle", fail)
    assert module.main() == 1
    line = capsys.readouterr().err.strip()
    assert "\n" not in line
    assert "synthetic canary/route-publication" in line
    assert "SliceError" in line and "GET /api/fleet returned HTTP 503" in line
