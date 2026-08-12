from __future__ import annotations

import copy
import importlib.util
import json
import subprocess
import sys
from importlib.machinery import SourceFileLoader
from pathlib import Path

import jsonschema
import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/workload-artifact-metadata"
SCHEMA = ROOT / "schemas/workload-artifact-build.schema.json"
COMMIT = "0123456789abcdef0123456789abcdef01234567"
SHA_A = "sha256:" + "a" * 64
SHA_B = "sha256:" + "b" * 64
SHA_C = "sha256:" + "c" * 64
SHA_D = "sha256:" + "d" * 64
DS4_REQUEST = ROOT / "release/workloads/ds4-v0.5.3-spark-runtime.json"
DS4_SOURCE_COMMIT = "b7737737aa2cd1246aa265687c8ba3d49d935bc3"
DS4_CONTEXT_DIGEST = (
    "sha256:a64b8fdccff10eaa9896902bbefa0015377ee54f9c67d548c4b3e3b40705b5bf"
)
DS4_IMAGE = (
    "ghcr.io/carstvaartjes/spark-ds4@sha256:"
    "084d9a9ffa47431842c5dec84de97b058034dec0535b2a563bc5db78c9e14615"
)
BUSYBOX_IMAGE = (
    "docker.io/library/busybox@sha256:"
    "fc6dddc4c44b1bfe37f41cae8e67d1693828e8f42a91862816d7953e2c9d3f23"
)


def _load_script():
    loader = SourceFileLoader("workload_artifact_metadata", str(SCRIPT))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _request_document() -> dict[str, object]:
    return {
        "architecture": "linux/arm64",
        "attestations": {"provenance": True, "sbom": True},
        "base_images": [f"nvcr.io/nvidia/cuda@{SHA_A}"],
        "context": "adapters/deepseek/ds4",
        "context_digest": SHA_B,
        "dockerfile": "adapters/deepseek/ds4/Dockerfile",
        "kind": "workload-artifact-build-request",
        "output_repository": "ghcr.io/carstvaartjes/vonk-forge-workloads",
        "schema_version": 1,
        "source_commit": COMMIT,
        "target": "runtime",
    }


def _result_document(build_request_digest: str) -> dict[str, object]:
    return {
        "build_request_digest": build_request_digest,
        "kind": "workload-artifact-build-result",
        "oci_manifest_digest": SHA_A,
        "provenance_digest": SHA_C,
        "sbom_digest": SHA_D,
        "schema_version": 1,
        "source_commit": COMMIT,
    }


def test_repository_contains_exact_reviewed_ds4_runtime_request() -> None:
    module = _load_script()
    document = json.loads(DS4_REQUEST.read_text(encoding="utf-8"))

    request = module.WorkloadArtifactBuild.parse(document)

    assert request.source_commit == DS4_SOURCE_COMMIT
    assert request.context_digest == DS4_CONTEXT_DIGEST
    assert request.context == "adapters/deepseek/ds4"
    assert request.dockerfile == "adapters/deepseek/ds4/Dockerfile.workload"
    assert request.target == "runtime"
    assert request.architecture == "linux/arm64"
    assert request.output_repository == "ghcr.io/carstvaartjes/vonk-forge-workloads"
    assert request.base_images == (BUSYBOX_IMAGE, DS4_IMAGE)


def test_request_parser_accepts_only_a_bounded_exact_build_contract() -> None:
    module = _load_script()

    request = module.WorkloadArtifactBuild.parse(_request_document())

    assert request.source_commit == COMMIT
    assert request.output_repository == "ghcr.io/carstvaartjes/vonk-forge-workloads"
    assert request.context == "adapters/deepseek/ds4"
    assert request.dockerfile == "adapters/deepseek/ds4/Dockerfile"
    assert request.digest == module.WorkloadArtifactBuild.parse(
        copy.deepcopy(_request_document())
    ).digest
    assert request.digest.startswith("sha256:")


def test_schema_and_parser_accept_the_same_request_and_result() -> None:
    module = _load_script()
    request_document = _request_document()
    request = module.WorkloadArtifactBuild.parse(request_document)
    result_document = _result_document(request.digest)

    schema = json.loads(SCHEMA.read_text())
    jsonschema.Draft202012Validator.check_schema(schema)
    jsonschema.validate(request_document, schema)
    jsonschema.validate(result_document, schema)
    module.WorkloadArtifactResult.parse(result_document, request=request)


@pytest.mark.parametrize("segment", (".", ".."))
def test_schema_and_parser_both_reject_dot_path_segments(segment: str) -> None:
    module = _load_script()
    document = _request_document()
    document["context"] = f"adapters/{segment}/ds4"
    document["dockerfile"] = f"adapters/{segment}/ds4/Dockerfile"
    schema = json.loads(SCHEMA.read_text())

    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(document, schema)
    with pytest.raises(ValueError, match="path"):
        module.WorkloadArtifactBuild.parse(document)


@pytest.mark.parametrize("value", (0, 1))
def test_schema_and_parser_both_require_real_attestation_booleans(value: int) -> None:
    module = _load_script()
    document = _request_document()
    document["attestations"]["provenance"] = value
    schema = json.loads(SCHEMA.read_text())

    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(document, schema)
    with pytest.raises(ValueError, match="attestations"):
        module.WorkloadArtifactBuild.parse(document)


@pytest.mark.parametrize("field", ["build_args", "credentials", "secrets", "token"])
def test_request_rejects_unreviewed_or_secret_bearing_inputs(field: str) -> None:
    module = _load_script()
    document = _request_document()
    document[field] = {"EXFILTRATE": "value"}

    with pytest.raises(ValueError, match="exact fields"):
        module.WorkloadArtifactBuild.parse(document)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("source_commit", COMMIT[:-1]),
        ("source_commit", COMMIT.upper()),
        ("context_digest", "b" * 64),
        ("context_digest", "sha256:" + "B" * 64),
        ("target", "x" * 129),
        ("output_repository", "ghcr.io/Owner/workload"),
        ("output_repository", "ghcr.io/" + "a" * 240 + "/workload"),
        ("architecture", []),
        ("base_images", []),
        ("base_images", [f"example.invalid/base@{SHA_A}"] * 17),
    ],
)
def test_request_rejects_missing_exact_or_unbounded_values(
    field: str, value: object
) -> None:
    module = _load_script()
    document = _request_document()
    document[field] = value

    with pytest.raises(ValueError):
        module.WorkloadArtifactBuild.parse(document)


@pytest.mark.parametrize(
    ("context", "dockerfile"),
    [
        ("../adapters/ds4", "../adapters/ds4/Dockerfile"),
        ("/adapters/ds4", "/adapters/ds4/Dockerfile"),
        ("adapters/ds4", "adapters/other/Dockerfile"),
        ("control", "control/Dockerfile"),
        ("adapters\\ds4", "adapters\\ds4\\Dockerfile"),
        (
            f"adapters/ds4\nsource_commit={COMMIT}",
            f"adapters/ds4\nsource_commit={COMMIT}/Dockerfile",
        ),
        ("adapters/ds4\rrequest_digest=sha256:" + "a" * 64, "adapters/ds4/Dockerfile"),
    ],
)
def test_request_rejects_path_escape_or_unreviewed_source_roots(
    context: str, dockerfile: str
) -> None:
    module = _load_script()
    document = _request_document()
    document["context"] = context
    document["dockerfile"] = dockerfile

    with pytest.raises(ValueError, match="path|root|context"):
        module.WorkloadArtifactBuild.parse(document)


@pytest.mark.parametrize(
    "reference",
    [
        "nvcr.io/nvidia/cuda:13.0.0-runtime-ubuntu24.04",
        "nvcr.io/nvidia/cuda:latest",
        f"nvcr.io/nvidia/cuda:13.0@{SHA_A}",
        f"NVCR.IO/nvidia/cuda@{SHA_A}",
    ],
)
def test_request_rejects_mutable_or_noncanonical_base_images(reference: str) -> None:
    module = _load_script()
    document = _request_document()
    document["base_images"] = [reference]

    with pytest.raises(ValueError, match="base image"):
        module.WorkloadArtifactBuild.parse(document)


def test_request_requires_sbom_and_provenance() -> None:
    module = _load_script()
    for attestations in (
        {"provenance": False, "sbom": True},
        {"provenance": True, "sbom": False},
        {"provenance": True, "sbom": True, "signature": True},
    ):
        document = _request_document()
        document["attestations"] = attestations
        with pytest.raises(ValueError, match="attestations"):
            module.WorkloadArtifactBuild.parse(document)


def test_result_requires_all_content_digests_and_binds_the_request() -> None:
    module = _load_script()
    request = module.WorkloadArtifactBuild.parse(_request_document())

    result = module.WorkloadArtifactResult.parse(
        _result_document(request.digest), request=request
    )

    assert result.build_request_digest == request.digest
    assert result.source_commit == request.source_commit
    assert result.oci_manifest_digest == SHA_A
    assert result.sbom_digest == SHA_D
    assert result.provenance_digest == SHA_C


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ({"build_request_digest": SHA_A}, "request digest"),
        ({"source_commit": "f" * 40}, "source commit"),
        ({"oci_manifest_digest": "a" * 64}, "OCI manifest digest"),
        ({"sbom_digest": None}, "SBOM digest"),
        ({"provenance_digest": "sha256:" + "F" * 64}, "provenance digest"),
        ({"registry_token": "secret"}, "exact fields"),
    ],
)
def test_result_fails_closed_for_unbound_incomplete_or_extra_evidence(
    mutation: dict[str, object], message: str
) -> None:
    module = _load_script()
    request = module.WorkloadArtifactBuild.parse(_request_document())
    document = _result_document(request.digest)
    document.update(mutation)

    with pytest.raises(ValueError, match=message):
        module.WorkloadArtifactResult.parse(document, request=request)


def test_cli_validates_the_request_and_bound_result(tmp_path: Path) -> None:
    module = _load_script()
    request_document = _request_document()
    request = module.WorkloadArtifactBuild.parse(request_document)
    request_path = tmp_path / "request.json"
    result_path = tmp_path / "result.json"
    request_path.write_text(json.dumps(request_document))
    result_path.write_text(json.dumps(_result_document(request.digest)))

    request_run = subprocess.run(
        [SCRIPT, "request", str(request_path)],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    result_run = subprocess.run(
        [SCRIPT, "result", str(result_path), "--request", str(request_path)],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert request_run.returncode == 0, request_run.stderr
    assert json.loads(request_run.stdout) == {
        "build_request_digest": request.digest,
        "request": request_document,
    }
    assert result_run.returncode == 0, result_run.stderr
    assert json.loads(result_run.stdout) == _result_document(request.digest)


def test_cli_rejects_duplicate_json_keys(tmp_path: Path) -> None:
    document = json.dumps(_request_document())
    duplicate = document.replace(
        f'"source_commit": "{COMMIT}"',
        f'"source_commit": "{COMMIT}", "source_commit": "{COMMIT}"',
    )
    request_path = tmp_path / "duplicate-request.json"
    request_path.write_text(duplicate)

    result = subprocess.run(
        [SCRIPT, "request", str(request_path)],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 2
    assert result.stdout == ""
    assert "duplicate JSON field" in result.stderr
