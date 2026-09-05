from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from vonk_control.distribution import (
    DistributionService,
    MemoryVerifiedObjectSource,
    ModelCacheVerifiedObjectSource,
)
from vonk_agent_protocol import DistributionAssignment, DistributionObject

from .test_agent_api import NODE_A, NODE_B, agent_headers, agent_system


def _assignment(
    node_id: str, model_digest: str, config_digest: str, archive_digest: str
) -> DistributionAssignment:
    return DistributionAssignment.parse(
        {
            "schema_version": 2,
            "assignment_id": str(uuid4()),
            "plan_digest": "a" * 64,
            "generation": 1,
            "node_id": node_id,
            "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
            # The fixture models the NAS cache's opaque manifest identity.
            "model_artifact_set_sha256": "b" * 64,
            "objects": [
                {"name": "weights/model.bin", "sha256": model_digest, "bytes": 13, "kind": "model"},
                {"name": "config/tokenizer.json", "sha256": config_digest, "bytes": 7, "kind": "model"},
                {"name": "image.oci.tar", "sha256": archive_digest, "bytes": 11, "kind": "oci-archive"},
            ],
            "oci_image_digest": "sha256:" + "d" * 64,
            "oci_archive_sha256": archive_digest,
        }
    )


def test_controller_serves_one_verified_assignment_to_two_nodes(agent_system) -> None:
    client, services, _, clock = agent_system
    source = MemoryVerifiedObjectSource()
    model_digest = source.put(b"model payload")
    config_digest = source.put(b"config!")
    archive_digest = source.put(b"oci archive")
    service = DistributionService(source, clock=clock)
    assignment = _assignment(NODE_A, model_digest, config_digest, archive_digest)
    source.register_artifact_set(assignment.model_artifact_set_sha256, assignment.objects)
    source.register_runtime_image(assignment.oci_image_digest, archive_digest)
    service.register(assignment)
    service.register(
        DistributionAssignment.parse(
            assignment.to_mapping() | {"assignment_id": str(uuid4()), "node_id": NODE_B}
        )
    )
    object.__setattr__(services, "distribution", service)

    manifest = client.get(
        "/agent/v1/distribution/manifests/" + "a" * 64,
        headers=agent_headers(NODE_A, "serial-a"),
    )
    assert manifest.status_code == 200
    assert {item["sha256"] for item in manifest.json()["objects"]} == {
        model_digest,
        config_digest,
        archive_digest,
    }
    response = client.get(
        "/agent/v1/distribution/objects/" + model_digest + "?plan_digest=" + "a" * 64,
        headers={**agent_headers(NODE_A, "serial-a"), "Range": "bytes=2-7"},
    )
    assert response.status_code == 206
    assert response.content == b"del pa"
    assert response.headers["etag"] == f'"sha256:{model_digest}"'
    assert response.headers["content-range"] == "bytes 2-7/13"
    assert client.get(
        "/agent/v1/distribution/objects/" + model_digest + "?plan_digest=" + "a" * 64,
        headers={**agent_headers(NODE_A, "serial-a"), "Range": "bytes=0-1,3-4"},
    ).status_code == 416

    second = client.get(
        "/agent/v1/distribution/objects/" + model_digest + "?plan_digest=" + "a" * 64,
        headers=agent_headers(NODE_B, "serial-b"),
    )
    assert second.status_code == 200 and second.content == b"model payload"


def test_distribution_rejects_unassigned_wrong_node_and_corrupt_object(agent_system) -> None:
    client, services, _, clock = agent_system
    source = MemoryVerifiedObjectSource()
    model_digest = source.put(b"model payload")
    archive_digest = source.put(b"oci archive")
    service = DistributionService(source, clock=clock)
    assignment = _assignment(NODE_A, model_digest, source.put(b"config!"), archive_digest)
    source.register_artifact_set(assignment.model_artifact_set_sha256, assignment.objects)
    source.register_runtime_image(assignment.oci_image_digest, archive_digest)
    service.register(assignment)
    object.__setattr__(services, "distribution", service)
    path = "/agent/v1/distribution/objects/" + model_digest
    assert client.get(path + "?plan_digest=" + "a" * 64, headers=agent_headers(NODE_B, "serial-b")).status_code == 403
    assert client.get(path + "?plan_digest=" + "e" * 64, headers=agent_headers(NODE_A, "serial-a")).status_code == 403
    source.objects[model_digest] = b"tampered payload"
    assert client.get(path + "?plan_digest=" + "a" * 64, headers=agent_headers(NODE_A, "serial-a")).status_code == 503


def test_distribution_assignment_survives_controller_service_restart(agent_system) -> None:
    _client, _services, _tokens, clock = agent_system
    sessions = _services.sessions
    source = MemoryVerifiedObjectSource()
    assignment = _assignment(
        NODE_A,
        source.put(b"model payload"),
        source.put(b"config!"),
        source.put(b"oci archive"),
    )
    source.register_artifact_set(assignment.model_artifact_set_sha256, assignment.objects)
    source.register_runtime_image(assignment.oci_image_digest, assignment.oci_archive_sha256)
    DistributionService(source, clock=clock, sessions=sessions).register(assignment)

    restarted = DistributionService(source, clock=clock, sessions=sessions)
    assert restarted.manifest(node_id=NODE_A, plan_digest=assignment.plan_digest)["assignment_id"] == assignment.assignment_id
    restarted.revoke(plan_digest=assignment.plan_digest, node_id=NODE_A)
    try:
        restarted.authorize(node_id=NODE_A, plan_digest=assignment.plan_digest)
    except Exception as error:
        assert getattr(error, "code", None) == "distribution.revoked"
    else:
        raise AssertionError("revoked assignment remained authorized")


def test_distribution_binds_opaque_cache_and_image_identities(agent_system) -> None:
    _client, _services, _tokens, clock = agent_system
    source = MemoryVerifiedObjectSource()
    assignment = _assignment(
        NODE_A,
        source.put(b"model payload"),
        source.put(b"config!"),
        source.put(b"oci archive"),
    )
    source.register_artifact_set("c" * 64, assignment.objects)
    source.register_runtime_image(assignment.oci_image_digest, assignment.oci_archive_sha256)
    try:
        DistributionService(source, clock=clock).register(assignment)
    except Exception as error:
        assert getattr(error, "code", None) == "distribution.model_set_mismatch"
    else:
        raise AssertionError("unverified cache manifest was accepted")


def test_model_cache_adapter_consumes_service_manifest_identity(tmp_path) -> None:
    payload = b"model payload"
    digest = __import__("hashlib").sha256(payload).hexdigest()
    path = tmp_path / "model.bin"
    path.write_bytes(payload)

    class Cache:
        class Manifest:
            digest = "b" * 64

        def manifest_for_artifact_set(self, set_digest):
            assert set_digest == "b" * 64
            return self.Manifest()

        def resolve_verified_artifact_set(self, set_digest):
            return ({"path": "weights/model.bin", "sha256": digest, "bytes": len(payload), "file": path},)

        def verified_artifact_file(self, set_digest, object_digest, object_path):
            assert set_digest == "b" * 64 and object_digest == digest and object_path == "weights/model.bin"
            return path, len(payload), digest

    source = ModelCacheVerifiedObjectSource.from_service(Cache())
    obj = DistributionObject("weights/model.bin", digest, len(payload), "model")
    assert source.verify_artifact_set("b" * 64, (obj,))
    opened = source.open_verified(digest, len(payload))
    assert opened.stream.read() == payload
