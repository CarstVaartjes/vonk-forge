from __future__ import annotations

import base64
import io
import json
from datetime import UTC, datetime
from hashlib import sha256
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from pydantic import ValidationError
from vonk_control import capture_deployment_observation as collector
from vonk_control.capture_deployment_observation import (
    API_REPOSITORY,
    CaptureError,
    CaptureInput,
    capture,
)
from vonk_control.deployment_provenance_contract import (
    DeploymentObservations,
    PhysicalAcceptanceReceipt,
    PlatformObservation,
)

SOURCE = "a" * 40
IMAGE_DIGEST = "b" * 64
CONTAINER_ID = "c" * 64
IMAGE_ID = "sha256:" + "d" * 64


def _release_document(*, source: str = SOURCE, digest: str = IMAGE_DIGEST):
    artifact = {"path": "test/object", "sha256": "e" * 64, "size": 1}
    package = {
        **artifact,
        "architecture": "linux-arm64",
        "host_signature": "f" * 128,
        "package_version": "1.0.0",
        "target_binary_digest": "1" * 64,
        "target_build_digest": "sha256:" + "2" * 64,
    }
    return {
        "artifacts": {
            "agent-package-linux-arm64": package,
            "agent-package-signature-linux-arm64": artifact,
            "cli-wheel": artifact,
            "nas-payload": artifact,
            "nas-setup-darwin-amd64": artifact,
            "nas-setup-darwin-arm64": artifact,
            "nas-setup-linux-amd64": artifact,
            "nas-setup-linux-arm64": artifact,
            "spark-setup-linux-arm64": artifact,
            "spark-setup-signature-linux-arm64": artifact,
        },
        "bootstraps": {"nas": artifact, "spark": artifact},
        "channel": "stable",
        "generation": "3" * 64,
        "images": {
            role: (
                f"ghcr.io/carstvaartjes/vonk-forge-{role}:v1.0.0@sha256:"
                f"{digest if role == 'api' else '4' * 64}"
            )
            for role in ("api", "worker", "hermes", "litellm")
        },
        "schema_version": 2,
        "source_sha": source,
        "version": "1.0.0",
    }


def _development_release_document(*, source: str, image_source: str, digest: str):
    document = _release_document(source=source, digest=digest)
    document["channel"] = "dev"
    document["version"] = "0.1.0~dev.1+g" + source[:12]
    document["images"] = {
        role: (
            f"ghcr.io/carstvaartjes/vonk-forge-{role}:dev-sha-{image_source}"
            f"@sha256:{digest if role == 'api' else '4' * 64}"
        )
        for role in ("api", "worker", "hermes", "litellm")
    }
    return document


def _signed_request(private_key, *, release=None, repo_digest=IMAGE_DIGEST):
    document = release or _release_document()
    release_raw = (
        json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode()
    signature = private_key.sign(release_raw, padding.PKCS1v15(), hashes.SHA256())
    return CaptureInput.model_validate(
        {
            "schema_version": 1,
            "release_json_base64": base64.b64encode(release_raw).decode(),
            "release_signature_base64": base64.b64encode(signature).decode(),
            "container": {
                "id": CONTAINER_ID,
                "image_id": IMAGE_ID,
                "hostname": CONTAINER_ID[:12],
                "configured_image": f"{API_REPOSITORY}:stable",
                "running": True,
                "health": "healthy",
            },
            "image": {
                "id": IMAGE_ID,
                "repo_digests": [f"{API_REPOSITORY}@sha256:{repo_digest}"],
            },
        }
    )


def _previous_observations(path, *, controller_id="9" * 64):
    previous = DeploymentObservations(
        repository=PlatformObservation(
            source="GitHub main",
            observed_at=datetime(2026, 9, 1, tzinfo=UTC),
            source_commit="8" * 40,
        ),
        publication=PlatformObservation(
            source="Accepted stable publication",
            observed_at=datetime(2026, 9, 2, tzinfo=UTC),
            source_commit="7" * 40,
            manifest_sha256="6" * 64,
        ),
        controller=PlatformObservation(
            source="Previous Controller instance",
            observed_at=datetime(2026, 9, 3, tzinfo=UTC),
            source_commit="5" * 40,
            image_digest="sha256:" + "4" * 64,
            container_id=controller_id,
        ),
        physical_acceptance=[
            PhysicalAcceptanceReceipt(
                source="Physical acceptance",
                observed_at=datetime(2026, 9, 4, tzinfo=UTC),
                evidence_sha256="3" * 64,
                run_id="run-1",
                run_generation=1,
                installation_id="installation-1",
                recipe_sha256="2" * 64,
                image_digest="sha256:" + "1" * 64,
                node_ids=["spk_" + "a" * 32],
                passed=True,
                lane="physical-spark",
            )
        ],
    )
    path.write_text(previous.model_dump_json(exclude_none=True) + "\n")
    return previous


@pytest.fixture(scope="module")
def release_signer(tmp_path_factory):
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
    public_key_path = tmp_path_factory.mktemp("release-key") / "public.pem"
    public_key_path.write_bytes(
        private_key.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    return private_key, public_key_path


@pytest.mark.parametrize("additional_manifest", [False, True])
def test_capture_records_actual_accepted_instance_and_preserves_other_evidence(
    tmp_path, release_signer, additional_manifest
):
    private_key, public_key_path = release_signer
    path = tmp_path / "observations.json"
    previous = _previous_observations(path)
    build_path = tmp_path / "controller-build.json"
    build_path.write_text(json.dumps({"source_commit": SOURCE}))
    release_raw = (
        json.dumps(_release_document(), sort_keys=True, separators=(",", ":")).encode()
        + b"\n"
    )

    request = _signed_request(private_key)
    if additional_manifest:
        request.image.repo_digests.insert(0, f"{API_REPOSITORY}@sha256:{'f' * 64}")
    result = capture(
        request,
        observation_path=path,
        build_metadata_path=build_path,
        public_key_path=public_key_path,
        runtime_hostname=CONTAINER_ID[:12],
        clock=lambda: datetime(2026, 9, 25, tzinfo=UTC),
    )

    stored = DeploymentObservations.model_validate_json(path.read_bytes())
    assert result == stored.controller
    assert result.source_commit == SOURCE
    assert result.image_digest == "sha256:" + IMAGE_DIGEST
    assert result.manifest_sha256 == sha256(release_raw).hexdigest()
    assert result.container_id == CONTAINER_ID
    assert result.observed_at == datetime(2026, 9, 25, tzinfo=UTC)
    assert stored.repository == previous.repository
    assert stored.publication == previous.publication
    assert stored.physical_acceptance == previous.physical_acceptance
    assert not list(tmp_path.glob(".observations-*"))


@pytest.mark.parametrize("mismatch", ["image", "source"])
def test_verified_release_identity_mismatch_clears_only_controller(
    tmp_path, release_signer, mismatch
):
    private_key, public_key_path = release_signer
    path = tmp_path / "observations.json"
    previous = _previous_observations(path)
    build_path = tmp_path / "controller-build.json"
    build_path.write_text(
        json.dumps({"source_commit": "5" * 40 if mismatch == "source" else SOURCE})
    )
    request = _signed_request(
        private_key,
        repo_digest=("e" * 64 if mismatch == "image" else IMAGE_DIGEST),
    )

    with pytest.raises(CaptureError, match="does not match"):
        capture(
            request,
            observation_path=path,
            build_metadata_path=build_path,
            public_key_path=public_key_path,
            runtime_hostname=CONTAINER_ID[:12],
        )

    stored = DeploymentObservations.model_validate_json(path.read_bytes())
    assert stored.controller is None
    assert stored.repository == previous.repository
    assert stored.publication == previous.publication
    assert stored.physical_acceptance == previous.physical_acceptance


def test_development_release_accepts_an_attested_reused_ancestor_image(
    tmp_path, release_signer
):
    private_key, public_key_path = release_signer
    path = tmp_path / "observations.json"
    image_source = "9" * 40
    release_source = "8" * 40
    build_path = tmp_path / "controller-build.json"
    build_path.write_text(json.dumps({"source_commit": image_source}))
    request = _signed_request(
        private_key,
        release=_development_release_document(
            source=release_source, image_source=image_source, digest=IMAGE_DIGEST
        ),
    )

    result = capture(
        request,
        observation_path=path,
        build_metadata_path=build_path,
        public_key_path=public_key_path,
        runtime_hostname=CONTAINER_ID[:12],
    )

    assert result.source_commit == image_source
    assert result.image_digest == "sha256:" + IMAGE_DIGEST


def test_unrelated_or_unhealthy_container_cannot_replace_observation(
    tmp_path, release_signer
):
    private_key, public_key_path = release_signer
    path = tmp_path / "observations.json"
    previous = _previous_observations(path)
    build_path = tmp_path / "controller-build.json"
    build_path.write_text(json.dumps({"source_commit": SOURCE}))
    request = _signed_request(private_key)
    request = request.model_copy(
        update={"container": request.container.model_copy(update={"running": False})}
    )

    with pytest.raises(CaptureError, match="not running"):
        capture(
            request,
            observation_path=path,
            build_metadata_path=build_path,
            public_key_path=public_key_path,
            runtime_hostname=CONTAINER_ID[:12],
        )

    stored = DeploymentObservations.model_validate_json(path.read_bytes())
    assert stored.controller == previous.controller


def test_inspection_for_replaced_container_cannot_replace_observation(
    tmp_path, release_signer
):
    private_key, public_key_path = release_signer
    path = tmp_path / "observations.json"
    previous = _previous_observations(path)
    build_path = tmp_path / "controller-build.json"
    build_path.write_text(json.dumps({"source_commit": SOURCE}))

    with pytest.raises(CaptureError, match="not for this running Controller"):
        capture(
            _signed_request(private_key),
            observation_path=path,
            build_metadata_path=build_path,
            public_key_path=public_key_path,
            runtime_hostname="f" * 12,
        )

    stored = DeploymentObservations.model_validate_json(path.read_bytes())
    assert stored.controller == previous.controller


def test_invalid_signature_cannot_clear_or_publish_evidence(tmp_path, release_signer):
    private_key, public_key_path = release_signer
    path = tmp_path / "observations.json"
    previous = _previous_observations(path)
    before = path.read_bytes()
    build_path = tmp_path / "controller-build.json"
    build_path.write_text(json.dumps({"source_commit": SOURCE}))
    request = _signed_request(private_key)
    altered = bytearray(base64.b64decode(request.release_signature_base64))
    altered[-1] ^= 1
    request = request.model_copy(
        update={"release_signature_base64": base64.b64encode(altered).decode()}
    )

    with pytest.raises(CaptureError, match="signature is invalid"):
        capture(
            request,
            observation_path=path,
            build_metadata_path=build_path,
            public_key_path=public_key_path,
            runtime_hostname=CONTAINER_ID[:12],
        )

    assert path.read_bytes() == before
    assert (
        DeploymentObservations.model_validate_json(path.read_bytes()).controller
        == previous.controller
    )


def test_valid_signature_does_not_replace_full_release_schema_validation(
    tmp_path, release_signer
):
    private_key, public_key_path = release_signer
    path = tmp_path / "observations.json"
    previous = _previous_observations(path)
    build_path = tmp_path / "controller-build.json"
    build_path.write_text(json.dumps({"source_commit": SOURCE}))
    malformed_release = _release_document()
    del malformed_release["bootstraps"]

    with pytest.raises(CaptureError, match="contract or signing key"):
        capture(
            _signed_request(private_key, release=malformed_release),
            observation_path=path,
            build_metadata_path=build_path,
            public_key_path=public_key_path,
            runtime_hostname=CONTAINER_ID[:12],
        )

    assert DeploymentObservations.model_validate_json(path.read_bytes()).controller == (
        previous.controller
    )


def test_capture_input_rejects_full_inspection_secret_fields(release_signer):
    private_key, _ = release_signer
    request = _signed_request(private_key)
    document = request.model_dump(mode="json")
    document["container"]["environment"] = {"HF_TOKEN": "dummy-test-secret"}
    with pytest.raises(ValidationError):
        CaptureInput.model_validate(document)


def test_packaged_module_entrypoint_consumes_the_host_projection(
    tmp_path, release_signer, monkeypatch, capsys
):
    from vonk_control import deployment_provenance as provenance

    private_key, public_key_path = release_signer
    request = _signed_request(private_key)
    path = tmp_path / "observations.json"
    build_path = tmp_path / "controller-build.json"
    build_path.write_text(json.dumps({"source_commit": SOURCE}))
    monkeypatch.setenv("VONK_DEPLOYMENT_OBSERVATIONS_FILE", str(path))
    monkeypatch.setattr(collector, "PUBLIC_KEY_PATH", public_key_path)
    monkeypatch.setattr(collector, "CONTROLLER_BUILD_METADATA", build_path)
    monkeypatch.setattr(collector.socket, "gethostname", lambda: CONTAINER_ID[:12])
    monkeypatch.setattr(
        collector.sys,
        "stdin",
        SimpleNamespace(buffer=io.BytesIO(request.model_dump_json().encode())),
    )

    assert collector.main() == 0

    stored = DeploymentObservations.model_validate_json(path.read_bytes())
    assert stored.controller is not None
    assert stored.controller.image_digest == "sha256:" + IMAGE_DIGEST
    monkeypatch.setattr(provenance, "CONTROLLER_BUILD_METADATA", build_path)
    assert provenance.local_deployment_observations().controller == stored.controller
    assert (
        capsys.readouterr().out == f"recorded Controller image sha256:{IMAGE_DIGEST}\n"
    )
